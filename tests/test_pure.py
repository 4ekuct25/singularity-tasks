"""Быстрые проверки: ни сети, ни токена.

Покрыто то, что ломается молча и на глаз не видно: разбор Quill-дельты (в карточке
окажется сырой JSON вместо текста), нормализация заголовков, ширина колонки
держателя, приоритет 0, порядок очереди и главный инвариант скилла — перенос в
колонку подтверждается ПЕРЕЧИТЫВАНИЕМ, а не кодом ответа.

Запуск: tests/run.py fast
"""

import contextlib
import io
import json
import os
import datetime
import subprocess
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

sing = support.load_sing("sing_pure")


@contextlib.contextmanager
def quiet():
    """`die()` пишет в stderr — ловим текст вместо того, чтобы засорять вывод."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        yield err


# --------------------------------------------------------------------- заметка


class NoteDeltaTest(unittest.TestCase):
    """Заметка хранится как Quill-delta. Приложение понимает ГОЛЫЙ МАССИВ операций;
    объект-обёртку `{"ops": [...]}` оно не разбирает и показывает сырой JSON."""

    def test_reads_all_three_shapes(self):
        self.assertEqual(sing.note_ops(None), [])
        self.assertEqual(sing.note_ops(""), [])
        # уже разобранный список
        self.assertEqual(sing.note_ops([{"insert": "x"}]), [{"insert": "x"}])
        # правильная форма — массив в строке
        self.assertEqual(sing.note_ops('[{"insert": "x"}]'), [{"insert": "x"}])
        # объект-обёртка: читаем, но сами так не пишем
        self.assertEqual(sing.note_ops('{"ops": [{"insert": "x"}]}'), [{"insert": "x"}])
        # legacy — простой текст, не JSON
        self.assertEqual(sing.note_ops("просто текст"), [{"insert": "просто текст"}])
        # JSON, но не дельта: 42 — валидный JSON и при этом не список и не объект
        self.assertEqual(sing.note_ops("42"), [{"insert": "42"}])
        # битый JSON не должен ронять команду
        self.assertEqual(sing.note_ops('[{"insert": '), [{"insert": '[{"insert": '}])

    def test_to_text_skips_non_text_ops(self):
        ops = [{"insert": "а"}, {"insert": {"image": "..."}}, "мусор", {"insert": "б"}]
        self.assertEqual(sing.note_to_text(ops), "аб")

    def test_append_writes_bare_array_not_object(self):
        """Главный инвариант: на выходе МАССИВ. Объект приложение не разберёт."""
        raw = sing.note_append(None, "текст")
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, list)
        self.assertEqual(sing.note_to_text(raw), "текст\n")

    def test_append_normalises_object_form(self):
        """Пришла заметка в виде объекта — дописали и привели к массиву."""
        raw = sing.note_append('{"ops": [{"insert": "старое\\n"}]}', "новое")
        self.assertIsInstance(json.loads(raw), list)
        self.assertEqual(sing.note_to_text(raw), "старое\n\nновое\n")

    def test_append_separator_gives_exactly_one_blank_line(self):
        """Разделитель зависит от того, кончается ли прошлая операция переводом
        строки. Обе ветки обязаны дать ОДНУ пустую строку, а не две и не ноль."""
        with_nl = sing.note_append('[{"insert": "старое\\n"}]', "новое")
        without_nl = sing.note_append('[{"insert": "старое"}]', "новое")
        self.assertEqual(sing.note_to_text(with_nl), "старое\n\nновое\n")
        self.assertEqual(sing.note_to_text(without_nl), "старое\n\nновое\n")

    def test_label_is_a_separate_bold_op(self):
        """По метке `ПЛАН (agent:*)` cmd_done решает, бралась ли задача в работу."""
        raw = sing.note_append(None, "шаг", label="ПЛАН (agent:claude-tests)")
        ops = json.loads(raw)
        self.assertEqual(ops[0], {"insert": "ПЛАН (agent:claude-tests)",
                                  "attributes": {"bold": True}})
        self.assertEqual(ops[1], {"insert": ": "})
        self.assertIn("ПЛАН (", sing.note_to_text(raw))

    def test_bullets_hang_on_newline_not_on_text(self):
        """Блочный атрибут Quill вешается на "\\n". Перепутать легко, и тогда
        приложение нарисует абзац вместо списка."""
        ops = json.loads(sing.note_append(None, "- один\n* два\nобычная"))
        self.assertEqual(ops[0], {"insert": "один"})
        self.assertEqual(ops[1], {"insert": "\n", "attributes": {"list": "bullet"}})
        self.assertEqual(ops[2], {"insert": "два"})
        self.assertEqual(ops[3], {"insert": "\n", "attributes": {"list": "bullet"}})
        self.assertEqual(ops[4], {"insert": "обычная\n"})

    def test_dump_keeps_cyrillic_readable(self):
        self.assertIn("привет", sing.note_dump([{"insert": "привет"}]))


# --------------------------------------------------------------------- заголовки


class TitleTest(unittest.TestCase):
    """Клиент приложения оборачивает похожее на домен в <a href>, и после
    синхронизации тот же заголовок перестаёт совпадать побайтно."""

    def test_plain_unwraps_links_only(self):
        self.assertEqual(
            sing.plain('<a href="http://banlist-ufm-cft.md">banlist-ufm-cft.md</a>'),
            "banlist-ufm-cft.md")
        self.assertEqual(sing.plain(None), "")
        # угловые скобки сами по себе — осмысленный текст задачи, не разметка
        self.assertEqual(sing.plain("Live-check FR12: <details> → expand"),
                         "Live-check FR12: <details> → expand")
        self.assertEqual(sing.plain("<ac:structured-macro> в заголовке"),
                         "<ac:structured-macro> в заголовке")

    def test_same_title_ignores_html_case_and_spaces(self):
        self.assertTrue(sing.same_title(
            'Проверить <a href="http://ex.md">ex.md</a>', "проверить  ex.md"))
        self.assertTrue(sing.same_title("  A  B  ", "a b"))
        self.assertFalse(sing.same_title("Проверить ex.md", "Проверить ex.com"))
        self.assertTrue(sing.same_title(None, ""))


# --------------------------------------------------------------------- доска


class BoardHolderTest(unittest.TestCase):

    def test_pad_keeps_columns_aligned(self):
        pad = sing.board_pad({"T-1": "@codex", "T-2": "@qwen"})
        self.assertEqual(len(pad("@codex")), len(pad(None)))
        self.assertEqual(len(pad("@qwen")), len(pad(None)))
        self.assertTrue(pad("@codex").startswith("  @codex"))

    def test_pad_without_holders_adds_nothing(self):
        """Тегов на доске нет — вывод обязан совпасть с прежним байт в байт."""
        self.assertEqual(sing.board_pad({})("что угодно"), "  ")
        self.assertEqual(sing.board_pad({})(None), "  ")

    def test_pad_truncates_long_name_without_shifting_board(self):
        long = "@" + "x" * 40
        pad = sing.board_pad({"T-1": long})
        self.assertEqual(len(pad(long)), sing.HOLDER_COL_MAX + 4)
        self.assertTrue(pad(long).strip().endswith("…"))
        self.assertEqual(len(pad(None)), len(pad(long)))

    def test_holders_skips_tag_request_when_no_tags(self):
        """Доска нового проекта не должна дорожать ради заведомо пустой колонки."""
        calls = []
        self.addCleanup(setattr, sing, "paged", sing.paged)
        sing.paged = lambda *a, **kw: calls.append(a) or []
        self.assertEqual(sing.board_holders([{"id": "T-1"}, {"id": "T-2", "tags": []}]), {})
        self.assertEqual(calls, [], "лишний GET /tag на доске без тегов")

    def test_holders_lists_agents_sorted_and_ignores_other_tags(self):
        self.addCleanup(setattr, sing, "paged", sing.paged)
        sing.paged = lambda *a, **kw: [
            {"id": "A-1", "title": "agent:zeta"},
            {"id": "A-2", "title": "agent:alpha"},
            {"id": "A-3", "title": "важное"},
        ]
        holders = sing.board_holders([
            {"id": "T-1", "tags": ["A-1", "A-2", "A-3"]},
            {"id": "T-2", "tags": ["A-3"]},
        ])
        self.assertEqual(holders, {"T-1": "@alpha,zeta"})


class BoardLayoutTest(unittest.TestCase):
    """Раскладка доски по ролям. Ловушка, ради которой тест написан: ключ None в
    `by_col` — это задачи ВНЕ колонок, и роль, которой нет в привязке, получала
    именно их (T-278ad639). Доска показывала задачи, которых в этой колонке нет."""

    STATUSES = {"KS-T": "Новые", "KS-W": "В работе", "KS-D": "Готово",
                "KS-B": "Заблокировано"}

    def _by_col(self):
        return {
            "KS-T": [{"id": "T-1", "title": "в очереди"}],
            None: [{"id": "T-loose1", "title": "вне колонок"},
                   {"id": "T-loose2", "title": "вне колонок 2"}],
        }

    def _cfg(self, **columns):
        return {"projectId": "P-x", "columns": columns}

    def test_unbound_role_gets_nothing_not_the_loose_tasks(self):
        cfg = self._cfg(todo="KS-T", wip="KS-W", done="KS-D", blocked="KS-B")
        layout = {role: (cid, name, items)
                  for role, cid, name, items, _ in
                  sing.board_layout(cfg, self._by_col(), self.STATUSES, 10)}
        cid, name, items = layout["review"]
        self.assertIsNone(cid)
        self.assertEqual(items, [], "роль без колонки забрала задачи вне колонок")
        self.assertEqual(name, sing.UNBOUND_COLUMN)
        # соседние роли не пострадали
        self.assertEqual([t["id"] for t in layout["todo"][2]], ["T-1"])
        self.assertEqual(layout["todo"][1], "Новые")

    def test_every_missing_role_is_empty_not_a_copy_of_the_same_list(self):
        """Пустая привязка — пять ролей, и ни одна не повторяет чужой список."""
        layout = sing.board_layout(self._cfg(), self._by_col(), self.STATUSES, 10)
        self.assertEqual([r for r, *_ in layout], sing.COLUMN_ORDER)
        self.assertEqual([len(items) for *_, items, _ in layout], [0] * 5)

    def test_bound_role_with_dead_column_keeps_its_tasks(self):
        """Колонка в привязке есть, а в трекере её уже нет: это другой случай,
        задачи по ней показываем (их и чинить), а диагноз ставит doctor."""
        cfg = self._cfg(review="KS-GONE")
        by_col = dict(self._by_col(), **{"KS-GONE": [{"id": "T-9", "title": "x"}]})
        role, cid, name, items, _ = sing.board_layout(cfg, by_col, self.STATUSES, 10)[2]
        self.assertEqual((role, cid, name), ("review", "KS-GONE", "?"))
        self.assertEqual([t["id"] for t in items], ["T-9"])

    def test_done_is_cut_by_limit_but_count_stays_full(self):
        cfg = self._cfg(done="KS-D")
        by_col = {"KS-D": [{"id": f"T-{i}", "modificatedDate": f"2026-01-0{i}"}
                           for i in range(1, 5)]}
        *_, items, shown = sing.board_layout(cfg, by_col, self.STATUSES, 2)[3]
        self.assertEqual(len(items), 4, "счётчик колонки обязан считать все")
        self.assertEqual([t["id"] for t in shown], ["T-4", "T-3"], "показаны не свежие")


# --------------------------------------------------------------------- приоритет


class PriorityTest(unittest.TestCase):
    """priority 0 — это «высокий», а не «не задан». `or 1` здесь ловушка:
    ноль ложный, и вся очередь съезжает. Баг был реальным."""

    def test_zero_is_high_not_default(self):
        self.assertEqual(sing.prio_of({"priority": 0}), 0)
        self.assertEqual(sing.prio_of({}), 1)
        self.assertEqual(sing.prio_of({"priority": None}), 1)
        self.assertEqual(sing.prio_of({"priority": "2"}), 2)

    def test_brief_shows_high_for_zero(self):
        line = sing.brief({"id": "T-1", "priority": 0, "title": "x"})
        self.assertIn("!высокий", line)
        self.assertIn("T-1", line)

    def test_brief_cuts_deadline_to_date_and_unwraps_html(self):
        line = sing.brief({"id": "T-1", "deadline": "2026-01-02T10:00:00.000Z",
                           "title": '<a href="http://ex.md">ex.md</a>'})
        self.assertIn("дедлайн=2026-01-02", line)
        self.assertIn("ex.md", line)
        self.assertNotIn("<a", line)


class QueueOrderTest(unittest.TestCase):
    """Порядок очереди: приоритет → дедлайн → возраст. Сетей не трогаем —
    подменяем два сборщика данных."""

    def setUp(self):
        self.cfg = {"projectId": "P-x", "columns": {"todo": "KS-T"}}
        self.addCleanup(setattr, sing, "column_map", sing.column_map)
        self.addCleanup(setattr, sing, "open_tasks", sing.open_tasks)
        self.addCleanup(setattr, sing, "live_tasks", sing.live_tasks)

    def _serve(self, tasks):
        sing.column_map = lambda pid: {t["id"]: "KS-T" for t in tasks}
        sing.open_tasks = lambda pid: tasks
        sing.live_tasks = lambda pid: tasks

    def test_high_priority_first_then_deadline_then_age(self):
        self._serve([
            {"id": "T-low", "priority": 2, "createdDate": "2020"},
            {"id": "T-high", "priority": 0, "createdDate": "2030"},
            {"id": "T-norm-late", "priority": 1, "deadline": "2030-01-01",
             "createdDate": "2020"},
            {"id": "T-norm-soon", "priority": 1, "deadline": "2026-01-01",
             "createdDate": "2021"},
            {"id": "T-norm-old", "priority": 1, "createdDate": "2019"},
        ])
        order = [t["id"] for t in sing._pick_pool(self.cfg, "todo")]
        self.assertEqual(order[0], "T-high", "задача с приоритетом 0 не первая")
        self.assertEqual(order[1], "T-norm-soon")
        self.assertEqual(order[2], "T-norm-late")
        # без дедлайна — в хвост своей группы приоритета, но раньше низкого
        self.assertEqual(order[3], "T-norm-old")
        self.assertEqual(order[4], "T-low")


class TaskFilterTest(unittest.TestCase):
    """Что вообще попадает в выборку. journalDate намеренно НЕ закреплён: по нему
    открыта отдельная карточка («Закрытые задачи пропадают с доски»), и пинить
    сегодняшнее поведение значило бы заранее покрасить её решение в красный."""

    def setUp(self):
        self.addCleanup(setattr, sing, "fetch_tasks", sing.fetch_tasks)
        sing.fetch_tasks = lambda pid: [
            {"id": "T-open", "checked": 0},
            {"id": "T-done", "checked": 1},
            {"id": "T-removed", "checked": 0, "removed": True},
            {"id": "T-deleted", "checked": 0, "deleteDate": "2026-01-01"},
            {"id": "T-note", "checked": 0, "isNote": True},
        ]

    def test_live_tasks_keep_completed_but_drop_trash_and_notes(self):
        ids = [t["id"] for t in sing.live_tasks("P-x")]
        self.assertIn("T-open", ids)
        self.assertIn("T-done", ids, "выполненная задача обязана остаться на доске")
        self.assertNotIn("T-removed", ids)
        self.assertNotIn("T-deleted", ids)
        self.assertNotIn("T-note", ids)

    def test_open_tasks_drops_completed(self):
        self.assertEqual([t["id"] for t in sing.open_tasks("P-x")], ["T-open"])

    def test_project_notes_keeps_only_notes(self):
        self.assertEqual([t["id"] for t in sing.project_notes("P-x")], ["T-note"])


# --------------------------------------------------------------------- колонки


class ColumnTest(unittest.TestCase):

    def test_system_status_id_only_for_system_roles(self):
        self.assertEqual(sing.system_status_id("P-x", "todo"), "KS-P-x-TODO")
        self.assertEqual(sing.system_status_id("P-x", "wip"), "KS-P-x-IN-PROGRESS")
        self.assertEqual(sing.system_status_id("P-x", "done"), "KS-P-x-DONE")
        self.assertIsNone(sing.system_status_id("P-x", "review"))
        self.assertIsNone(sing.system_status_id("P-x", "blocked"))

    def test_desired_order_uses_live_neighbours(self):
        mapping = {"wip": "KS-W", "done": "KS-D"}
        statuses = [{"id": "KS-W", "kanbanOrder": 40000},
                    {"id": "KS-D", "kanbanOrder": 90000}]
        self.assertEqual(sing.desired_order("review", mapping, statuses), 65000)
        self.assertEqual(sing.desired_order("blocked", mapping, statuses), 140000)
        # соседей не видно — откат на фиксированную подсказку
        self.assertEqual(sing.desired_order("review", {}, []),
                         sing.COLUMN_ORDER_HINT["review"])

    def test_col_id_refuses_missing_role(self):
        with quiet() as err, self.assertRaises(SystemExit):
            sing.col_id({"columns": {"todo": "KS-T"}}, "review")
        self.assertIn("review", err.getvalue())


class MoveToColumnTest(unittest.TestCase):
    """AGENTS.md §4: этот API умеет ответить 200, ничего не сделав. Перенос обязан
    подтверждаться перечитыванием связки, а не кодом ответа."""

    def setUp(self):
        self.calls = []
        self.addCleanup(setattr, sing, "request", sing.request)
        self.addCleanup(setattr, sing, "board_links", sing.board_links)
        self.addCleanup(setattr, sing, "task_project", sing.task_project)
        # у задачи бывает связка с доской «Сегодня», поэтому move_to_column берёт
        # связки не как есть, а через board_links — по колонкам своего проекта
        sing.task_project = lambda tid: "P-1"
        self.addCleanup(setattr, sing, "task_column", sing.task_column)
        self.addCleanup(setattr, sing, "NET_BACKOFF", sing.NET_BACKOFF)
        sing.NET_BACKOFF = 0          # паузы в проверке ни к чему
        sing.request = lambda m, p, **kw: self.calls.append((m, p)) or {}

    def _links(self, link):
        sing.board_links = (lambda tid, project_id=None, include_removed=False:
                            [link] if link else [])

    def _columns(self, *sequence):
        it = iter(sequence)
        last = [None]

        # project_id добавился, когда task_column научился выбирать связку доски
        # СВОЕГО проекта: у задачи бывает ещё связка с доской «Сегодня»
        def col(tid, project_id=None):
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return last[0]

        sing.task_column = col

    def test_noop_when_already_there(self):
        self._columns("KS-T")
        self._links({"id": "KTS-1"})
        self.assertEqual(sing.move_to_column("T-1", "KS-T"), "уже в колонке")
        self.assertEqual(self.calls, [], "лишний запрос на задаче, уже стоящей в колонке")

    def test_patches_existing_link(self):
        self._columns("KS-OLD", "KS-T")
        self._links({"id": "KTS-1"})
        self.assertEqual(sing.move_to_column("T-1", "KS-T"), "перемещена")
        self.assertEqual(self.calls, [("PATCH", "/kanban-task-status/KTS-1")])

    def test_recreates_link_when_column_was_deleted(self):
        self._columns(None, "KS-T")
        self._links({"id": "KTS-1", "removed": True})
        self.assertEqual(sing.move_to_column("T-1", "KS-T"), "привязана к колонке")
        self.assertEqual(self.calls, [("POST", "/kanban-task-status")])

    def test_silent_200_is_a_failure_not_a_success(self):
        """Сервер отвечает 200, связка не меняется. Это обязано быть отказом."""
        self._columns("KS-OLD")          # колонка не меняется никогда
        self._links({"id": "KTS-1"})
        with quiet() as err, self.assertRaises(SystemExit):
            sing.move_to_column("T-1", "KS-T")
        self.assertIn("перенос не применился", err.getvalue())
        self.assertEqual(len(self.calls), sing.NET_RETRIES,
                         "повторов не столько, сколько объявлено NET_RETRIES")

    def test_non_fatal_returns_none_so_caller_can_name_the_task(self):
        """`add` обязан назвать уже созданный T-id, иначе сироту не найти."""
        self._columns("KS-OLD")
        self._links({"id": "KTS-1"})
        self.assertIsNone(sing.move_to_column("T-1", "KS-T", fatal=False))


# --------------------------------------------------------------------- окружение


class AgentNameTest(unittest.TestCase):

    def test_precedence_flag_env_config_default(self):
        with support.env(SINGULARITY_AGENT="from-env"):
            self.assertEqual(sing.agent_name({"agent": "from-cfg"}, "from-flag"),
                             "from-flag")
            self.assertEqual(sing.agent_name({"agent": "from-cfg"}), "from-env")
        with support.env(SINGULARITY_AGENT=None):
            self.assertEqual(sing.agent_name({"agent": "from-cfg"}), "from-cfg")
            self.assertEqual(sing.agent_name(None), "claude")


class ConfigLookupTest(unittest.TestCase):
    """Привязка ищется в .agents/, затем .claude/, затем в корне — и вверх по дереву."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sing-cfg-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def _put(self, rel):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path) or self.tmp, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"projectId": "P-x"}, f)
        return path

    def test_agents_wins_over_claude_and_root(self):
        self._put("singularity.json")
        self._put(".claude/singularity.json")
        want = self._put(".agents/singularity.json")
        self.assertEqual(sing.find_config(self.tmp), want)

    def test_claude_still_read_when_agents_absent(self):
        want = self._put(".claude/singularity.json")
        self.assertEqual(sing.find_config(self.tmp), want)

    def test_walks_up_from_subdirectory(self):
        want = self._put(".agents/singularity.json")
        deep = os.path.join(self.tmp, "a", "b", "c")
        os.makedirs(deep)
        self.assertEqual(sing.find_config(deep), want)

    def test_nothing_found_is_not_a_false_hit(self):
        found = sing.find_config(self.tmp) or ""
        self.assertFalse(found.startswith(self.tmp))


class ProjectChainTest(unittest.TestCase):

    def test_survives_a_cycle(self):
        """Цикл parent→parent в данных не должен вешать команду."""
        projects = [{"id": "A", "parent": "B"}, {"id": "B", "parent": "A"}]
        chain = sing.project_chain("A", projects)
        self.assertEqual([p["id"] for p in chain], ["A", "B"])

    def test_walks_up_to_root(self):
        projects = [{"id": "C", "parent": "B"}, {"id": "B", "parent": "A"},
                    {"id": "A"}]
        self.assertEqual([p["id"] for p in sing.project_chain("C", projects)],
                         ["C", "B", "A"])


if __name__ == "__main__":
    unittest.main()


class RenameTaskTest(unittest.TestCase):
    """AGENTS.md §4: 200 ничего не доказывает. Переименование обязано
    подтверждаться перечитыванием и не задевать остальное состояние задачи."""

    def setUp(self):
        self.addCleanup(setattr, sing, "request", sing.request)
        self.addCleanup(setattr, sing, "TAG_SETTLE_PAUSE", sing.TAG_SETTLE_PAUSE)
        sing.TAG_SETTLE_PAUSE = 0          # паузы в проверке ни к чему
        self.patched = []

    def _server(self, reads):
        """reads — что отдаёт GET после PATCH, по одному на перечитывание."""
        it = iter(reads)
        last = [reads[0]]

        def req(method, path, **kw):
            if method == "PATCH":
                self.patched.append(kw.get("body"))
                return {}
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return last[0]

        sing.request = req

    def test_confirms_by_reread_not_by_status(self):
        task = {"title": "старый", "checked": 0, "tags": ["A-1"]}
        self._server([dict(task, title="новый")])
        old, saved = sing.rename_task("T-1", "новый", task)
        self.assertEqual((old, saved), ("старый", "новый"))
        self.assertEqual(self.patched, [{"title": "новый"}],
                         "PATCH обязан нести только title")

    def test_waits_out_a_lagging_queue(self):
        """Первое чтение отдаёт старое — это лаг очереди, а не отказ."""
        task = {"title": "старый", "checked": 0, "tags": []}
        self._server([task, task, dict(task, title="новый")])
        self.assertEqual(sing.rename_task("T-1", "новый", task)[1], "новый")

    def test_dies_when_title_never_applies(self):
        task = {"title": "старый", "checked": 0, "tags": []}
        self._server([task])
        with self.assertRaises(SystemExit):
            sing.rename_task("T-1", "новый", task)

    def test_dies_when_rename_touches_anything_else(self):
        """PATCH с лишним полем стирает состояние — это обязано быть замечено."""
        task = {"title": "старый", "checked": 1, "tags": ["A-1"]}
        self._server([{"title": "новый", "checked": 0, "tags": ["A-1"]}])
        with self.assertRaises(SystemExit):
            sing.rename_task("T-1", "новый", task)

    def test_returns_saved_title_not_the_sent_one(self):
        """Клиент приложения нормализует заголовок — показывать надо сохранённое."""
        task = {"title": "старый", "checked": 0, "tags": []}
        self._server([{"title": "новый", "checked": 0, "tags": []}])
        self.assertEqual(sing.rename_task("T-1", "новый ", task)[1], "новый")


class TagClobberTest(unittest.TestCase):
    """Список тегов правится чтением-записью, CAS этот API не умеет. Между GET и
    PATCH другой агент успевает добавить метку — запись старым списком её стирала,
    а подтверждение этого не замечало, потому что смотрело только на свои теги."""

    def setUp(self):
        self.addCleanup(setattr, sing, "request", sing.request)
        self.addCleanup(setattr, sing, "TAG_SETTLE_PAUSE", sing.TAG_SETTLE_PAUSE)
        sing.TAG_SETTLE_PAUSE = 0
        self.bodies = []

    def _server(self, reads):
        it = iter(reads); last = [reads[0]]

        def req(method, path, **kw):
            if method == "PATCH":
                self.bodies.append(kw.get("body", {}).get("tags"))
                return {}
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return {"tags": last[0]}

        sing.request = req

    def test_foreign_tag_added_mid_flight_is_merged_not_wiped(self):
        """Чужая метка появилась после первого чтения — она обязана уцелеть."""
        self._server([[], ["A-чужой"], ["A-чужой", "A-мой"]])
        self.assertTrue(sing.set_task_tags("T-1", add=["A-мой"]))
        self.assertEqual(self.bodies, [["A-мой", "A-чужой"]],
                         "запись обязана нести и чужую метку, а не только свою")

    def test_losing_a_foreign_tag_is_reported_not_swallowed(self):
        """Если чужая метка всё же пропала — это потеря следа, а не успех."""
        self._server([[], ["A-чужой"], ["A-мой"], ["A-мой"], ["A-мой"], ["A-мой"]])
        with self.assertRaises(SystemExit):
            sing.set_task_tags("T-1", add=["A-мой"])

    def test_own_drop_still_works(self):
        """Снятие своей метки не должно ломаться подмешиванием."""
        self._server([["A-мой", "A-чужой"], ["A-мой", "A-чужой"], ["A-чужой"]])
        self.assertTrue(sing.set_task_tags("T-1", drop=["A-мой"]))
        self.assertEqual(self.bodies, [["A-чужой"]])


class DetectAgentTest(unittest.TestCase):
    """Имя агента определяется само. Забытый export означал бы, что все
    подписываются одинаково, а на метке «кто взял задачу» держится защита
    от гонки — то есть тихо ломается именно то, ради чего метка нужна."""

    def setUp(self):
        self.addCleanup(setattr, sing, "__file__", sing.__file__)
        for v in ("SINGULARITY_AGENT", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
            self.addCleanup(os.environ.pop, v, None)
            os.environ.pop(v, None)

    def _running_from(self, path):
        sing.__file__ = path

    def test_every_install_dir_is_recognised(self):
        home = os.path.expanduser("~")
        for marker, expected in sing.AGENT_BY_SKILL_DIR:
            self._running_from(os.path.join(home, marker, "scripts", "sing.py"))
            self.assertEqual(sing.detect_agent(), expected, f"не опознан {marker}")

    def test_install_dirs_cover_every_deploy_target(self):
        """TARGETS в install.sh и таблица здесь обязаны не расходиться:
        иначе новый инструмент подпишется чужим именем."""
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sing.__file__))),
                              "tools", "install.sh")
        if not os.path.exists(script):
            self.skipTest("install.sh рядом нет (запуск из установленной копии)")
        targets = {line.split("|")[0].strip()
                   for line in open(script, encoding="utf-8")
                   if line.count("|") == 2 and "$HOME" in line}
        known = {name for _, name in sing.AGENT_BY_SKILL_DIR}
        self.assertEqual(targets - known, set(),
                         "цель раскатки есть, а распознавания имени для неё нет")

    def test_unknown_dir_falls_back_to_session_marker(self):
        self._running_from("/tmp/где-то/sing.py")
        self.assertIsNone(sing.detect_agent())
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(sing.detect_agent(), "claude")

    def test_explicit_override_wins_over_detection(self):
        self._running_from(os.path.join(os.path.expanduser("~"), ".qwen", "skills",
                                        "singularity-tasks", "scripts", "sing.py"))
        self.assertEqual(sing.agent_name(), "qwen")
        self.assertEqual(sing.agent_name(cfg={"agent": "из-конфига"}), "из-конфига",
                         "заданное человеком не должно перебиваться догадкой по среде")
        os.environ["SINGULARITY_AGENT"] = "свой"
        self.assertEqual(sing.agent_name(), "свой")
        self.assertEqual(sing.agent_name(override="из-флага"), "из-флага")


class RepoProjectNameTest(unittest.TestCase):
    """Имя проекта по умолчанию — имя репозитория. Промпт, где надо подставить
    значение руками, подставляют неправильно или забывают вовсе."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_plain_directory_gives_its_own_name(self):
        d = os.path.join(self.tmp, "мой-репозиторий")
        os.makedirs(d)
        self.assertEqual(sing.repo_project_name(d), "мой-репозиторий")

    def test_subdirectory_of_a_repo_gives_the_repo_name(self):
        """Запуск из tools/ не должен дать проект «tools»."""
        repo = os.path.join(self.tmp, "репо")
        sub = os.path.join(repo, "tools")
        os.makedirs(sub)
        subprocess.run(["git", "init", "-q", repo], check=True)
        self.assertEqual(sing.repo_project_name(sub), "репо")

    def test_trailing_separator_does_not_eat_the_name(self):
        d = os.path.join(self.tmp, "хвост")
        os.makedirs(d)
        self.assertEqual(sing.repo_project_name(d + os.sep), "хвост")


class AgentsRuleTest(unittest.TestCase):
    """После привязки правило «работа по доске» обязано оказаться в правилах
    репозитория: агент, зашедший в него, узнаёт о доске оттуда, а не из промпта."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.cfg = os.path.join(self.repo, ".agents", "singularity.json")

    def _apply(self):
        return sing.ensure_agents_rule(self.repo, "проект", self.cfg, apply=True)

    def _read(self, name):
        with open(os.path.join(self.repo, name), encoding="utf-8") as f:
            return f.read()

    def test_creates_both_files_in_an_empty_repo(self):
        self._apply()
        self.assertIn(sing.AGENTS_MARK, self._read("AGENTS.md"))
        self.assertEqual(self._read("CLAUDE.md").strip(), "@AGENTS.md",
                         "Claude Code читает CLAUDE.md — без заглушки правило не видно")

    def test_second_run_does_not_duplicate(self):
        self._apply()
        self.assertEqual(self._apply(), [], "повторный запуск обязан быть пустым")
        self.assertEqual(self._read("AGENTS.md").count(sing.AGENTS_MARK), 1)

    def test_existing_rules_are_kept(self):
        with open(os.path.join(self.repo, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# Свои правила\n\nНе трогать vendor/.\n")
        self._apply()
        text = self._read("AGENTS.md")
        self.assertIn("Не трогать vendor/.", text, "чужие правила затёрты")
        self.assertLess(text.index("Не трогать"), text.index(sing.AGENTS_MARK))

    def test_foreign_claude_md_is_warned_about_not_overwritten(self):
        with open(os.path.join(self.repo, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write("Чужие правила, не заглушка.\n")
        done = self._apply()
        self.assertEqual(self._read("CLAUDE.md"), "Чужие правила, не заглушка.\n")
        self.assertTrue(any("не ссылается на AGENTS.md" in d for d in done),
                        "молчаливое «не видно правила» хуже предупреждения")


class InitReusesExistingBindingTest(unittest.TestCase):
    """Повторный init (например, чтобы дописать правило в AGENTS.md) обязан брать
    проект из привязки, а не из имени каталога: они совпадают не всегда, и репозиторий
    уехал бы на другой проект или упёрся в «проекта нет»."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)

    def _bind(self, where, title, pid="P-известный"):
        d = os.path.join(self.repo, where)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "singularity.json"), "w", encoding="utf-8") as f:
            json.dump({"projectId": pid, "projectTitle": title, "columns": {}}, f)

    def test_binding_wins_over_directory_name(self):
        self._bind(".agents", "совсем-другое-имя")
        cfg_path = sing.find_config(self.repo)
        self.assertTrue(cfg_path.startswith(self.repo))
        with open(cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["projectTitle"], "совсем-другое-имя")
        self.assertNotEqual(sing.repo_project_name(self.repo), "совсем-другое-имя",
                            "проверка бессмысленна, если имена совпали")

    def test_legacy_claude_binding_is_found_too(self):
        self._bind(".claude", "старая-привязка")
        self.assertTrue(sing.find_config(self.repo).endswith(
            os.path.join(".claude", "singularity.json")))


class NotReadyReasonTest(unittest.TestCase):
    """`next` не должен предлагать то, что человек явно отодвинул: «отложить» и
    «начать такого-то числа» — его решения, и очередь, их игнорирующая, перестаёт
    быть очередью."""

    def setUp(self):
        self.today = datetime.date.today().isoformat()

    def test_deferred_is_not_offered(self):
        self.assertEqual(sing.not_ready_reason({"deferred": True}), "отложена")

    def test_future_start_is_not_offered(self):
        self.assertEqual(sing.not_ready_reason({"start": "2099-01-01T00:00:00.000Z"}),
                         "начало 2099-01-01")

    def test_today_is_ready_even_late_in_the_day(self):
        """start приходит полным ISO со временем: сравнение строк целиком
        отложило бы задачу «на сегодня 23:59» до завтра."""
        self.assertIsNone(sing.not_ready_reason({"start": self.today + "T23:59:00.000Z"}))

    def test_past_start_and_plain_task_are_ready(self):
        self.assertIsNone(sing.not_ready_reason({"start": "2020-01-01"}))
        self.assertIsNone(sing.not_ready_reason({}))
        self.assertIsNone(sing.not_ready_reason({"deferred": False, "start": ""}))

    def test_pool_hides_them_only_when_asked(self):
        """board и list обязаны показывать такие задачи: исчезнувшая карточка
        выглядит потерянной."""
        tasks = [{"id": "T-1", "deferred": True}, {"id": "T-2"}]
        ready = [t for t in tasks if not sing.not_ready_reason(t)]
        self.assertEqual([t["id"] for t in ready], ["T-2"])
        self.assertEqual(len(tasks), 2, "из общей выборки задачи не исчезают")


class OpenChildrenTest(unittest.TestCase):
    """Родитель — это его подзадачи. Взять его раньше них значит либо сделать их
    работу мимо доски, либо закрыть заголовок, под которым осталось незакрытое."""

    def test_counts_only_unfinished_children(self):
        tasks = [{"id": "P"},
                 {"id": "A", "parent": "P", "checked": 0},
                 {"id": "B", "parent": "P", "checked": 1},
                 {"id": "C", "checked": 0}]
        self.assertEqual(sing.open_children_counts(tasks), {"P": 1})

    def test_parent_waits_for_children(self):
        self.assertEqual(sing.not_ready_reason({"id": "P"}, open_children=2),
                         "ждёт подзадач: 2")

    def test_parent_is_ready_once_children_are_closed(self):
        self.assertIsNone(sing.not_ready_reason({"id": "P"}, open_children=0))

    def test_subtask_itself_is_offered(self):
        """Подзадача — обычная работа, её брать можно и нужно."""
        self.assertIsNone(sing.not_ready_reason({"id": "A", "parent": "P"}))

    def test_deferred_beats_children_in_the_message(self):
        """Причина одна и самая сильная: отложенное не «ждёт подзадач»."""
        self.assertEqual(sing.not_ready_reason({"deferred": True}, open_children=3),
                         "отложена")


class RecurrenceTest(unittest.TestCase):
    """Шаблон серии — правило её порождения, а не задача: закрыть его как обычную
    значит тронуть всю серию. Экземпляры серии — обычная работа."""

    def test_series_template_is_not_offered(self):
        t = {"recurrence": {"repeat": {"type": "EVERYDAY"}}}
        self.assertEqual(sing.not_ready_reason(t),
                         "повторяющаяся: это шаблон серии, а не задача")

    def test_instance_of_a_series_is_ordinary_work(self):
        self.assertIsNone(sing.not_ready_reason({"recurrenceGeneratorId": "T-серия"}))

    def test_future_instance_still_waits_its_date(self):
        self.assertEqual(
            sing.not_ready_reason({"recurrenceGeneratorId": "T-серия",
                                   "start": "2099-01-01T00:00:00.000Z"}),
            "начало 2099-01-01")

    def test_template_is_hidden_by_its_own_right_not_by_a_future_date(self):
        """В базе 30 из 31 шаблона имели дату в будущем и скрывались случайно.
        Шаблон без даты обязан скрываться сам по себе."""
        self.assertIsNotNone(sing.not_ready_reason({"recurrence": {}, "start": ""}))


class EffectiveColumnTest(unittest.TestCase):
    """Задача из приложения приходит без kanban-связки, а приложение показывает её
    в «Новые» — проверено на канбане проекта. Очередь обязана видеть то же самое,
    иначе агент говорит «пусто» при непустом проекте."""

    CFG = {"columns": {"todo": "KS-TODO", "wip": "KS-WIP", "done": "KS-DONE"}}

    def test_unlinked_open_task_counts_as_todo(self):
        self.assertEqual(
            sing.effective_column({"id": "T-1"}, {}, self.CFG), "KS-TODO")

    def test_existing_link_wins(self):
        self.assertEqual(
            sing.effective_column({"id": "T-1"}, {"T-1": "KS-WIP"}, self.CFG), "KS-WIP")

    def test_finished_task_without_link_is_not_dragged_into_todo(self):
        """Закрытую и унесённую в дневник приложение в «Новые» тоже не кладёт."""
        self.assertIsNone(
            sing.effective_column({"id": "T-1", "checked": 1}, {}, self.CFG))
        self.assertIsNone(
            sing.effective_column({"id": "T-2", "journalDate": "2026-09-14"}, {},
                                  self.CFG))

    def test_no_todo_in_binding_means_no_guess(self):
        """Привязка неполная — придумывать колонку нельзя."""
        self.assertIsNone(sing.effective_column({"id": "T-1"}, {}, {"columns": {}}))
