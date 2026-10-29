#!/usr/bin/env python3
"""tools/fix-split-board.py — полный apply-прогон на стоме sing.

Живая доска не трогается (карта запрещает запуск на неё): сетевой слой
(load_config, project_statuses, column_map, move_to_column, save_config,
request) заменён памятью, а настоящие plan_column_orders /
column_order_problems / SYSTEM_SUFFIX оставлены — логика порядка живёт в них,
и её покрывают собственные тесты.

Проверяется ровно то, что карта требует:
  * старые колонки снимаемых ролей попадают в план и удаляются тем же
    прогоном, когда опустели (financial_portfolio 10-24: план без единого
    УДАЛИТЬ, три пустые колонки удалялись руками);
  * порядок ролей выравнивается тем же прогоном (plan_column_orders +
    PATCH kanbanOrder) и сверяется перечитыванием: перечитывание не сошлось
    — exit 1;
  * колонку с задачами не удаляют; системные колонки никогда не попадают в
    stale, даже если снятая роль указывала на системную.
"""

import contextlib
import io
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

REAL_SING = support.load_sing("sing_fix_split_board")
PID = "P-1"
SYS_TODO = f"KS-{PID}-TODO"
SYS_WIP = f"KS-{PID}-IN-PROGRESS"
SYS_DONE = f"KS-{PID}-DONE"


def _restore_sing_module(prev):
    if prev is None:
        sys.modules.pop("sing", None)
    else:
        sys.modules["sing"] = prev


def load_tool_with(board):
    """Инструмент с фейковым `sing`, говорящим с FakeBoard."""
    fake = types.ModuleType("sing")
    fake.SYSTEM_SUFFIX = REAL_SING.SYSTEM_SUFFIX
    fake.plan_column_orders = REAL_SING.plan_column_orders
    fake.column_order_problems = REAL_SING.column_order_problems
    fake.load_config = board.load_config
    fake.project_statuses = board.project_statuses
    fake.column_map = board.column_map
    fake.move_to_column = board.move_to_column
    fake.save_config = board.save_config
    fake.request = board.request
    prev = sys.modules.get("sing")
    sys.modules["sing"] = fake
    try:
        return support.load_tool("fix-split-board.py",
                                 name="fix_split_board_under_test")
    finally:
        _restore_sing_module(prev)


def load_real_tool():
    """Инструмент с настоящим sing (только для чистых функций, main не ходит)."""
    prev = sys.modules.get("sing")
    try:
        return support.load_tool("fix-split-board.py", name="fix_split_board_real")
    finally:
        _restore_sing_module(prev)


TOOL = load_real_tool()


class FakeBoard:
    """Канбан в памяти: колонки {id: {...}}, задачи {id задачи: id колонки}."""

    def __init__(self, cfg, columns, tasks, delete_tasks=None,
                 patch_ignores=False):
        self.cfg = cfg
        self.columns = {cid: dict(c) for cid, c in columns.items()}
        self.tasks = dict(tasks)
        self.delete_tasks = delete_tasks   # карта для фазы удаления (2-й вызов)
        self.patch_ignores = patch_ignores  # PATCH отвечает 200 и не пишет
        self.cmap_calls = 0
        self.moved = []
        self.deleted = []
        self.patched = []
        self.saved = None

    def load_config(self):
        return self.cfg, "/fake/singularity.json"

    def project_statuses(self, pid):
        return [dict(c) for c in self.columns.values()]

    def column_map(self, pid):
        self.cmap_calls += 1
        if self.cmap_calls >= 2 and self.delete_tasks is not None:
            return dict(self.delete_tasks)
        return dict(self.tasks)

    def move_to_column(self, tid, sid, fatal=True, project_id=None):
        self.moved.append((tid, sid))
        self.tasks[tid] = sid
        return True

    def save_config(self, cfg, path):
        self.saved = dict(cfg)

    def request(self, method, path, body=None, soft=False):
        cid = path.rsplit("/", 1)[-1]
        if method == "GET":
            col = self.columns.get(cid)
            return dict(col) if col else None
        if method == "DELETE":
            if cid in self.columns:
                self.columns[cid]["removed"] = True
                self.deleted.append(cid)
            return None
        if method == "PATCH":
            col = self.columns.get(cid)
            if (col is not None and body and "kanbanOrder" in body
                    and not self.patch_ignores):
                col["kanbanOrder"] = body["kanbanOrder"]
                self.patched.append((cid, body["kanbanOrder"]))
            return dict(col) if col else None
        raise AssertionError(f"неожиданный запрос: {method} {path}")


def split_columns():
    """Раздвоение: свои колонки для todo/wip/done + системные рядом.
    Порядок перекошен: «На проверке» и «Готово» делят 100000 (как на
    financial_portfolio после инцидента)."""
    return {
        SYS_TODO: {"id": SYS_TODO, "name": "Новые", "kanbanOrder": 0},
        SYS_WIP: {"id": SYS_WIP, "name": "В работе", "kanbanOrder": 50000},
        SYS_DONE: {"id": SYS_DONE, "name": "Готово", "kanbanOrder": 100000},
        "KTS-A": {"id": "KTS-A", "name": "К работе", "kanbanOrder": 1},
        "KTS-B": {"id": "KTS-B", "name": "В работе (скилл)", "kanbanOrder": 51000},
        "KTS-C": {"id": "KTS-C", "name": "Готово (скилл)", "kanbanOrder": 100000},
        "KTS-R": {"id": "KTS-R", "name": "На проверке", "kanbanOrder": 100000},
        "KTS-BL": {"id": "KTS-BL", "name": "Заблокировано", "kanbanOrder": 150000},
    }


def split_cfg():
    return {"projectId": PID,
            "columns": {"todo": "KTS-A", "wip": "KTS-B", "done": "KTS-C",
                        "review": "KTS-R", "blocked": "KTS-BL"},
            "columnNames": {"todo": "К работе", "wip": "В работе (скилл)",
                            "done": "Готово (скилл)", "review": "На проверке",
                            "blocked": "Заблокировано"}}


class ApplyFlowTest(unittest.TestCase):
    """Полный прогон main() на стоме: план, apply, сверки."""

    # ⚠ не `run`: это имя метода TestCase, и раннер передаст сюда свой
    # TextTestResult вместо board (то же случилось с хелпером в test_install).
    def run_tool(self, board, *args):
        self.board = board
        tool = load_tool_with(board)
        argv = sys.argv
        sys.argv = ["fix-split-board.py"] + list(args)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                tool.main()
        except SystemExit as e:
            # sys.exit("текст") печатает текст в stderr только на верхнем уровне
            # интерпретатора — в тесте он остаётся в e.code, переносим сюда
            if isinstance(e.code, int):
                code = e.code
            else:
                code = 1
                err.write(str(e.code) + "\n")
        finally:
            sys.argv = argv
        return code, out.getvalue(), err.getvalue()

    def test_split_full_apply(self):
        """Один --apply: задачи перевезены, привязка переписана, три пустые
        свои колонки удалены, порядок выровнен — всё тем же прогоном."""
        board = FakeBoard(split_cfg(), split_columns(),
                          {"T1": "KTS-A", "T2": "KTS-B", "T3": "KTS-C"})
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(board.moved, [("T1", SYS_TODO), ("T2", SYS_WIP),
                                       ("T3", SYS_DONE)])
        self.assertEqual(board.saved["columns"],
                         {"todo": SYS_TODO, "wip": SYS_WIP, "done": SYS_DONE,
                          "review": "KTS-R", "blocked": "KTS-BL"})
        self.assertEqual(board.deleted, ["KTS-A", "KTS-B", "KTS-C"])
        # 100000 делили «На проверке» и «Готово»: «Готово» и «Заблокировано»
        # раздвинуты шагом 50000
        self.assertEqual(board.patched, [(SYS_DONE, 150000), ("KTS-BL", 200000)])
        self.assertEqual(out.count("удалена: True"), 3, out)
        self.assertIn("привязка переписана", out)
        self.assertIn("подвинута колонка «Готово» -> порядок 150000", out)

    def test_plan_changes_nothing(self):
        board = FakeBoard(split_cfg(), split_columns(), {"T1": "KTS-A"})
        code, out, err = self.run_tool(board)
        self.assertEqual(code, 0, out + err)
        self.assertIn("План (ничего не изменено", out)
        self.assertIn("УДАЛИТЬ опустевшую колонку «К работе» KTS-A", out)
        self.assertIn("СДВИНУТЬ колонку «Готово» -> порядок 150000", out)
        self.assertEqual(board.moved, [])
        self.assertEqual(board.deleted, [])
        self.assertEqual(board.patched, [])
        self.assertIsNone(board.saved)

    def test_nonempty_old_column_is_not_deleted(self):
        """Проверка left остаётся: колонка, в которой на фазе удаления
        нашлись задачи, не удаляется."""
        board = FakeBoard(split_cfg(), split_columns(),
                          {"T1": "KTS-A", "T2": "KTS-B", "T3": "KTS-C"},
                          delete_tasks={"T1": "KTS-A"})
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(board.deleted, ["KTS-B", "KTS-C"])
        self.assertIn("НЕ удаляю", out)
        self.assertIn("T1", out)
        self.assertNotIn("KTS-A", board.deleted)

    def test_stale_without_split(self):
        """Привязка уже на системных, но осталась лишняя своя колонка —
        один прогон её убирает."""
        columns = {
            SYS_TODO: {"id": SYS_TODO, "name": "Новые", "kanbanOrder": 0},
            SYS_WIP: {"id": SYS_WIP, "name": "В работе", "kanbanOrder": 50000},
            SYS_DONE: {"id": SYS_DONE, "name": "Готово", "kanbanOrder": 100000},
            "KTS-R": {"id": "KTS-R", "name": "На проверке", "kanbanOrder": 75000},
            "KTS-BL": {"id": "KTS-BL", "name": "Заблокировано",
                       "kanbanOrder": 150000},
            "KTS-X": {"id": "KTS-X", "name": "Архив", "kanbanOrder": 1},
        }
        cfg = {"projectId": PID,
               "columns": {"todo": SYS_TODO, "wip": SYS_WIP, "done": SYS_DONE,
                           "review": "KTS-R", "blocked": "KTS-BL"},
               "columnNames": {}}
        board = FakeBoard(cfg, columns, {})
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("УДАЛИТЬ опустевшую колонку «Архив» KTS-X", out)
        self.assertNotIn("ПЕРЕВЕЗТИ", out)
        self.assertEqual(board.deleted, ["KTS-X"])
        self.assertEqual(board.patched, [])

    def test_order_only_fix(self):
        """Ничего не раздвоено, но роли делят место (100000 у review и done)
        — один прогон выравнивает порядок."""
        columns = {
            SYS_TODO: {"id": SYS_TODO, "name": "Новые", "kanbanOrder": 0},
            SYS_WIP: {"id": SYS_WIP, "name": "В работе", "kanbanOrder": 50000},
            SYS_DONE: {"id": SYS_DONE, "name": "Готово", "kanbanOrder": 100000},
            "KTS-R": {"id": "KTS-R", "name": "На проверке", "kanbanOrder": 100000},
            "KTS-BL": {"id": "KTS-BL", "name": "Заблокировано",
                       "kanbanOrder": 150000},
        }
        cfg = {"projectId": PID,
               "columns": {"todo": SYS_TODO, "wip": SYS_WIP, "done": SYS_DONE,
                           "review": "KTS-R", "blocked": "KTS-BL"},
               "columnNames": {}}
        board = FakeBoard(cfg, columns, {})
        code, out, err = self.run_tool(board)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("УДАЛИТЬ", out)
        self.assertNotIn("ПЕРЕВЕЗТИ", out)
        self.assertIn("СДВИНУТЬ колонку «Готово» -> порядок 150000", out)
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(board.patched, [(SYS_DONE, 150000), ("KTS-BL", 200000)])
        self.assertEqual(board.deleted, [])

    def test_missing_system_columns_exits(self):
        columns = {
            "KTS-A": {"id": "KTS-A", "name": "Новые", "kanbanOrder": 0},
            "KTS-B": {"id": "KTS-B", "name": "В работе", "kanbanOrder": 1},
            "KTS-C": {"id": "KTS-C", "name": "Готово", "kanbanOrder": 2},
            "KTS-R": {"id": "KTS-R", "name": "На проверке", "kanbanOrder": 3},
            "KTS-BL": {"id": "KTS-BL", "name": "Заблокировано",
                       "kanbanOrder": 4},
        }
        cfg = {"projectId": PID,
               "columns": {"todo": "KTS-A", "wip": "KTS-B", "done": "KTS-C",
                           "review": "KTS-R", "blocked": "KTS-BL"},
               "columnNames": {}}
        board = FakeBoard(cfg, columns, {})
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Системных колонок в проекте нет", err)
        self.assertEqual(board.deleted, [])
        self.assertIsNone(board.saved)

    def test_board_ok(self):
        columns = {
            SYS_TODO: {"id": SYS_TODO, "name": "Новые", "kanbanOrder": 0},
            SYS_WIP: {"id": SYS_WIP, "name": "В работе", "kanbanOrder": 50000},
            SYS_DONE: {"id": SYS_DONE, "name": "Готово", "kanbanOrder": 100000},
            "KTS-R": {"id": "KTS-R", "name": "На проверке", "kanbanOrder": 75000},
            "KTS-BL": {"id": "KTS-BL", "name": "Заблокировано",
                       "kanbanOrder": 150000},
        }
        cfg = {"projectId": PID,
               "columns": {"todo": SYS_TODO, "wip": SYS_WIP, "done": SYS_DONE,
                           "review": "KTS-R", "blocked": "KTS-BL"},
               "columnNames": {}}
        board = FakeBoard(cfg, columns, {"T1": SYS_TODO})
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Доска в порядке.", out)
        self.assertEqual(board.deleted, [])
        self.assertEqual(board.patched, [])

    def test_order_verify_failure_exits_nonzero(self):
        """PATCH ответил 200, но порядок не изменился (эхо без записи) —
        сверка перечитыванием обязана поймать и не замолчать."""
        board = FakeBoard(split_cfg(), split_columns(),
                          {"T1": "KTS-A", "T2": "KTS-B", "T3": "KTS-C"},
                          patch_ignores=True)
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("порядок не выровнялся", err)

    def test_system_column_never_stale(self):
        """Снятая роль указывала на системную колонку другой роли — её нельзя
        удалить: она остаётся занятой (и она системная)."""
        columns = {
            SYS_TODO: {"id": SYS_TODO, "name": "Новые", "kanbanOrder": 0},
            SYS_WIP: {"id": SYS_WIP, "name": "В работе", "kanbanOrder": 50000},
            SYS_DONE: {"id": SYS_DONE, "name": "Готово", "kanbanOrder": 100000},
            "KTS-R": {"id": "KTS-R", "name": "На проверке", "kanbanOrder": 75000},
            "KTS-BL": {"id": "KTS-BL", "name": "Заблокировано",
                       "kanbanOrder": 150000},
        }
        cfg = {"projectId": PID,
               "columns": {"todo": SYS_TODO, "wip": SYS_TODO, "done": SYS_DONE,
                           "review": "KTS-R", "blocked": "KTS-BL"},
               "columnNames": {}}
        board = FakeBoard(cfg, columns, {"T1": SYS_TODO})
        code, out, err = self.run_tool(board, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(board.deleted, [])
        self.assertEqual(board.saved["columns"]["wip"], SYS_WIP)
        for sid in (SYS_TODO, SYS_WIP, SYS_DONE):
            self.assertNotIn(sid, board.deleted)


class PlanSplitFixTest(unittest.TestCase):
    """Чистая функция: stale считается от привязки ПОСЛЕ перепривязки."""

    def test_rebind_columns_become_stale(self):
        sysmap = {"todo": "KS-X-TODO", "wip": "KS-X-IN-PROGRESS",
                  "done": "KS-X-DONE"}
        live = list(sysmap.values()) + ["KTS-A", "KTS-B", "KTS-C"]
        cfg = {"todo": "KTS-A", "wip": "KTS-B", "done": "KTS-C",
               "review": "KTS-R", "blocked": "KTS-BL"}
        wrong, final, stale = TOOL.plan_split_fix(live, cfg, sysmap)
        self.assertEqual(set(wrong), {"todo", "wip", "done"})
        self.assertEqual(final["todo"], "KS-X-TODO")
        self.assertEqual(final["review"], "KTS-R")
        self.assertEqual(stale, ["KTS-A", "KTS-B", "KTS-C"])
        # старая формула (от текущей привязки) дала бы пусто — это и был баг
        old_formula = [cid for cid in live
                       if cid not in set(cfg.values())
                       and cid not in set(sysmap.values())]
        self.assertEqual(old_formula, [])

    def test_shared_and_system_columns_never_stale(self):
        sysmap = {"todo": "KS-X-TODO", "wip": "KS-X-IN-PROGRESS",
                  "done": "KS-X-DONE"}
        live = list(sysmap.values()) + ["KTS-A"]
        cfg = {"todo": "KS-X-TODO", "wip": "KS-X-TODO", "done": "KTS-A",
               "review": "KTS-R", "blocked": "KTS-BL"}
        _wrong, _final, stale = TOOL.plan_split_fix(live, cfg, sysmap)
        # KTS-A — старая колонка done, становится сиротой; системные и
        # занятая todo-колонка — нет
        self.assertEqual(stale, ["KTS-A"])


if __name__ == "__main__":
    unittest.main()
