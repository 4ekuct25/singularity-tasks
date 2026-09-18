"""Живые проверки: настоящий трекер, черновой подпроект на `zz-`, уборка за собой.

Проверяется РЕАЛЬНЫЙ путь — команды запускаются подпроцессом через `scripts/sing.py`,
а не импортированными функциями: соседний путь даёт уверенный, но ложный ответ.
Заодно так проверяются коды выхода и argparse, а именно ими пользуется агент.

⚠️ УБОРКА. Черновой проект удаляется в `addClassCleanup`, зарегистрированном
СРАЗУ после создания, — не в `tearDownClass`. Разница принципиальная: если
`setUpClass` упадёт после создания проекта (например на 429), `tearDownClass`
не вызовется вообще, а зарегистрированная уборка отработает. Ровно так в этой
сессии черновик провисел несколько часов.

Название черновика уникально на прогон (`zz-selftest-<время>-<pid>`): сметать
всё по маске `zz-*` нельзя — рядом живут черновики других сессий.

Запуск: tests/run.py live   (нужен токен и доступ к трекеру)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

AGENT = "zz-tester"
OTHER_AGENT = "zz-other"
# Теги в SingularityApp общие на аккаунт, поэтому пробные тоже убираются за собой.
TEST_TAG_PREFIX = "agent:zz-"

# Один черновой проект на ВЕСЬ модуль, а не на класс: каждый `sing.py` — это
# ещё и запросы к живому API, а create/delete проекта стоит дороже всего.
LIVE = {"sing": None, "zz": None, "proj": None, "cols": None, "workdir": None}


def draft_title():
    return f"zz-selftest-{int(time.time())}-{os.getpid()}"


def setUpModule():
    sing = support.load_sing("sing_live")
    # Проверь саму проверку: быстрая группа выставляет SINGULARITY_API на
    # localhost. Уйти туда живым набором — это позеленеть, ничего не проверив.
    if "127.0.0.1" in sing.API or "localhost" in sing.API:
        raise unittest.SkipTest(f"живые проверки нацелены на заглушку: {sing.API}")
    LIVE["sing"] = sing
    LIVE["zz"] = support.load_tool("zz-project.py")

    title = draft_title()
    try:
        proj, cols = LIVE["zz"].create_draft(sing, title, with_columns=True)
    except SystemExit as e:
        # `request()` на 5xx для POST намеренно не повторяет (повтор создал бы
        # второй проект), поэтому сюда прилетает die(). Переводим на понятный
        # язык: набор не «сломался», трекер отказался писать.
        raise RuntimeError(
            "не удалось завести черновой проект — трекер отказал в записи "
            f"(код {e.code}, причина выше). Обычно это перегруженная очередь "
            "синхронизации: `500 Sync error … number in queue N`. Ничего не "
            "создано и убирать нечего, просто повтори прогон позже.") from e
    # ⚠ запоминаем СРАЗУ: с этой секунды уборка обязана состояться, что бы
    # ни упало ниже. tearDownModule при падении setUpModule не вызывается,
    # поэтому зовём его руками.
    LIVE["proj"], LIVE["cols"] = proj, cols
    print(f"\n  черновой проект: {proj['id']}  {title}", file=sys.stderr)
    try:
        workdir = tempfile.mkdtemp(prefix="zz-selftest-")
        LIVE["workdir"] = workdir
        os.makedirs(os.path.join(workdir, ".agents"))
        with open(os.path.join(workdir, ".agents", "singularity.json"), "w") as f:
            json.dump({"projectId": proj["id"], "projectTitle": title,
                       "columns": cols,
                       "columnNames": dict(sing.DEFAULT_COLUMNS)}, f)
    except BaseException:
        tearDownModule()
        raise


def tearDownModule():
    """Уборка идемпотентна и переживает частичный setUpModule."""
    sing, zz = LIVE.get("sing"), LIVE.get("zz")
    if LIVE.get("workdir"):
        shutil.rmtree(LIVE["workdir"], ignore_errors=True)
        LIVE["workdir"] = None
    if sing and LIVE.get("proj"):
        proj = LIVE["proj"]
        LIVE["proj"] = None
        hit, left = zz.delete_draft(sing, proj["id"])
        print(f"  черновой проект удалён: {hit['id']} "
              f"(черновиков zz-* осталось {left})", file=sys.stderr)
    if sing:
        report_test_tags(sing)


def report_test_tags(sing):
    """Пробные теги `agent:zz-*` убрать НЕЧЕМ — и это не недосмотр, а свойство API.

    `DELETE /tag/{id}` отвечает `500 Sync error`, `PATCH {"removed": true}` тоже не
    проходит (замер и подробности — references/api.md). Поэтому мусор ограничен не
    уборкой, а конструкцией: имена пробных агентов ФИКСИРОВАНЫ, их ровно два, и
    `ensure_tag` переиспользует уже заведённые. Сколько бы раз набор ни прогнали,
    новых строк в общем списке тегов не появится.

    Функция ничего не удаляет молча — она считает и докладывает, чтобы рост
    (признак того, что где-то завелось имя с номером прогона) был виден сразу.
    """
    live = sorted(t["title"] for t in sing.paged("/tag", "tags")
                  if not t.get("removed")
                  and t.get("title", "").startswith(TEST_TAG_PREFIX))
    expected = {f"agent:{AGENT}", f"agent:{OTHER_AGENT}"}
    extra = [t for t in live if t not in expected]
    print(f"  пробных тегов {TEST_TAG_PREFIX}*: {len(live)} "
          f"(удалить их API не даёт — см. references/api.md)"
          + (f"; не от этого набора: {extra}" if extra else ""), file=sys.stderr)


class LiveBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sing, cls.cols = LIVE["sing"], LIVE["cols"]
        cls.proj, cls.workdir = LIVE["proj"], LIVE["workdir"]

    # ------------------------------------------------------------------ помощники

    def cli(self, *argv, expect=0, agent=AGENT):
        """Запустить настоящую команду скилла в привязанном черновом репозитории."""
        env = support.clean_env(SINGULARITY_AGENT=agent)
        p = subprocess.run(
            [sys.executable, support.SING, *argv],
            cwd=self.workdir, env=env, capture_output=True, text=True)
        if expect is not None:
            self.assertEqual(
                p.returncode, expect,
                f"sing.py {' '.join(argv)} -> код {p.returncode}\n"
                f"stdout: {p.stdout}\nstderr: {p.stderr}")
        return p

    def make_task(self, title, column="todo"):
        """Задача напрямую через API — дешевле, чем гонять `add` ради подготовки."""
        t = self.sing.request("POST", "/task",
                              body={"title": title, "projectId": self.proj["id"]})
        if column:
            self.sing.move_to_column(t["id"], self.cols[column])
        return t["id"]

    def column_of(self, task_id):
        """Роль колонки по факту, перечитыванием связки."""
        cid = self.sing.task_column(task_id)
        return next((r for r, v in self.cols.items() if v == cid), cid)

    def note_of(self, task_id):
        return self.sing.note_to_text(
            self.sing.request("GET", f"/task/{task_id}").get("note"))

    def agent_tags(self, task_id):
        task = self.sing.request("GET", f"/task/{task_id}")
        return sorted(t for _, t in self.sing.agent_tags_on(task))

    def checked(self, task_id):
        return int(self.sing.request("GET", f"/task/{task_id}").get("checked") or 0)


class AddTest(LiveBase):

    def test_add_puts_task_in_todo_with_description(self):
        title = "zz: задача из add"
        out = self.cli("add", title, "--note", "критерий: видно на доске").stdout
        tid = out.split(":")[0].strip()
        self.assertTrue(tid.startswith("T-"), out)
        self.assertEqual(self.column_of(tid), "todo")
        self.assertIn("критерий: видно на доске", self.note_of(tid))

    def test_add_refuses_a_second_task_with_the_same_title(self):
        """Оборвавшийся `add` оставлял сироту, агент не смотрел доску и заводил
        дубль. Проверяется и то, что HTML в заголовке не обманывает сравнение."""
        title = "zz: дубль ловится"
        self.make_task(title)
        p = self.cli("add", title, "--no-note", expect=1)
        self.assertIn("уже открыта", p.stderr)

    def test_add_demands_a_description(self):
        p = self.cli("add", "zz: без описания", expect=1)
        self.assertIn("нужно описание", p.stderr)

    def test_short_deadline_is_accepted_by_the_server(self):
        """Голую дату API отвергает `400`, и `add` падал сырым дампом ответа, не
        создав задачу (T-daa1e87c). Проверяется ФАКТОМ: поле перечитывается из
        трекера, а не берётся из кода ответа — 200 здесь ничего не доказывает.

        Прогонять надо именно живьём: разбор даты можно проверить на заглушке, а
        вот что сервер берёт получившуюся форму — только здесь.
        """
        out = self.cli("add", "zz: дедлайн короткой датой",
                       "--note", "критерий: задача создалась",
                       "--deadline", "2026-10-15").stdout
        tid = out.split(":")[0].strip()
        self.assertTrue(tid.startswith("T-"), out)
        saved = self.sing.request("GET", f"/task/{tid}").get("deadline")
        self.assertTrue(saved, "дедлайн не сохранился, а команда не упала")
        self.assertEqual(saved[:10], "2026-10-15",
                         f"календарный день съехал: {saved}")
        # доска печатает первые 10 символов — человек должен видеть ту же дату
        self.assertIn("дедлайн=2026-10-15", self.cli("list").stdout)

    def test_full_iso_deadline_is_accepted_as_written(self):
        out = self.cli("add", "zz: дедлайн полным ISO",
                       "--note", "критерий: своя таймзона не потерялась",
                       "--deadline", "2026-10-15T18:00:00+03:00").stdout
        tid = out.split(":")[0].strip()
        saved = self.sing.request("GET", f"/task/{tid}").get("deadline")
        self.assertTrue(saved, "дедлайн не сохранился, а команда не упала")
        self.assertEqual(saved[:10], "2026-10-15", f"день съехал: {saved}")

    def test_broken_deadline_is_refused_before_the_request(self):
        """Отказ обязан быть локальным и внятным: ни дампа ответа API, ни
        задачи-сироты, которую потом никто не найдёт."""
        title = "zz: дедлайн из несуществующей даты"
        p = self.cli("add", title, "--note", "не должна создаться",
                     "--deadline", "2026-02-30", expect=1)
        self.assertIn("--deadline", p.stderr)
        self.assertIn("2026-10-15", p.stderr, "в отказе нет примера формата")
        self.assertNotIn("HTTP 400", p.stderr, "наружу утёк ответ API")
        self.assertNotIn(title, self.cli("board").stdout,
                         "задача всё-таки создалась")


class SetFieldsTest(LiveBase):
    """`--priority`/`--deadline` были только у `add`: у созданной карточки эти поля
    из CLI не менялись, и понизить приоритет можно было либо руками в приложении,
    либо пересозданием — с потерей id, тегов `agent:*` и истории (T-62ba2372).

    Проверяется ФАКТОМ: поле перечитывается из трекера. Ответ `200` тут ничего не
    доказывает — на системных колонках этот же API так и делает."""

    def field(self, task_id, name):
        return self.sing.request("GET", f"/task/{task_id}").get(name)

    def test_priority_changes_on_an_existing_task(self):
        tid = self.make_task("zz: приоритет существующей задачи")
        out = self.cli("set", tid, "--priority", "0").stdout
        self.assertIn("приоритет", out)
        self.assertEqual(self.field(tid, "priority"), 0,
                         "приоритет не записался, а команда не упала")
        line = next(s for s in self.cli("list").stdout.splitlines() if tid in s)
        self.assertIn("[!высокий]", line, "доска показывает прежний приоритет")
        # то же значение второй раз не пишется вовсе: подтверждать перечитыванием
        # там нечего, поле равно ожидаемому и до запроса
        self.assertIn("уже", self.cli("set", tid, "--priority", "0").stdout)
        # и обратно: не разовый эффект, а нормальная правка
        self.cli("set", tid, "--priority", "2")
        self.assertEqual(self.field(tid, "priority"), 2)

    def test_deadline_is_set_and_then_cleared(self):
        """Снятие дедлайна обязано отличаться от «не трогать»: `--deadline ''`
        пишет null, отсутствие флага не отправляет поле вовсе."""
        tid = self.make_task("zz: дедлайн существующей задачи")
        self.cli("set", tid, "--deadline", "2026-10-15")
        self.assertEqual((self.field(tid, "deadline") or "")[:10], "2026-10-15")

        # правка соседнего поля дедлайн не трогает
        self.cli("set", tid, "--priority", "0")
        self.assertEqual((self.field(tid, "deadline") or "")[:10], "2026-10-15",
                         "дедлайн уехал вместе с приоритетом")

        self.cli("set", tid, "--deadline", "")
        self.assertFalse(self.field(tid, "deadline"),
                         "дедлайн не снялся, а команда отчиталась успехом")
        self.assertEqual(self.field(tid, "priority"), 0,
                         "снятие дедлайна задело приоритет")

    def test_set_keeps_the_task_intact(self):
        """PATCH с лишним полем стирает состояние. Взятая в работу задача обязана
        остаться взятой: тег держателя, колонка и заметка на месте."""
        tid = self.make_task("zz: правка не ломает задачу")
        self.cli("start", tid, "--plan", "проверяю правку полей")
        self.cli("set", tid, "--priority", "0", "--deadline", "2026-10-15")
        self.assertEqual(self.agent_tags(tid), [f"agent:{AGENT}"])
        self.assertEqual(self.column_of(tid), "wip")
        self.assertIn(f"ПЛАН (agent:{AGENT})", self.note_of(tid))

    def test_refusals_do_not_touch_the_task(self):
        """Отказ обязан быть локальным: ни дампа ответа API, ни половинчатой
        правки. Обе ветки на одной задаче — живой прогон и так упирается в
        rate limit, лишняя пара запросов здесь дороже отдельного теста."""
        tid = self.make_task("zz: отказы правки полей")
        p = self.cli("set", tid, expect=1)
        self.assertIn("нечего менять", p.stderr)

        p = self.cli("set", tid, "--deadline", "завтра", expect=1)
        self.assertIn("--deadline", p.stderr)
        self.assertNotIn("HTTP 400", p.stderr, "наружу утёк ответ API")
        self.assertFalse(self.field(tid, "deadline"))
        self.assertEqual(self.field(tid, "priority") or 1, 1,
                         "отказ всё-таки что-то записал")


class RegroupTest(LiveBase):
    """Перенос между секциями. Живьём проверяется ровно то, чего заглушка знать не
    может: что сервер БЕРЁТ посланное значение и что «вне секций» у него устроено
    так, как мы думаем, — это id безымянной служебной группы, а не пусто."""

    def group_of(self, task_id):
        return self.sing.request("GET", f"/task/{task_id}").get("group")

    def test_task_moves_into_a_section_and_back_outside(self):
        out = self.cli("groups", "--create", "zz-секция").stdout
        gid = out.rsplit("->", 1)[1].strip()
        self.assertTrue(gid.startswith("Q-"), out)
        tid = self.make_task("zz: перенос между секциями")
        loose = self.group_of(tid)
        # у только что созданной задачи group УЖЕ не пуст — это служебная группа
        self.assertTrue(loose, "«вне секций» оказалось пустым — замер устарел")
        self.assertEqual(loose, self.sing.fake_group(self.proj["id"]))

        self.cli("regroup", tid, "zz-секция")
        self.assertEqual(self.group_of(tid), gid,
                         "секция не записалась, а команда отчиталась успехом")
        self.assertIn("уже", self.cli("regroup", tid, "zz-секция").stdout)

        self.cli("regroup", tid, "--clear")
        self.assertEqual(self.group_of(tid), loose,
                         "снятие секции не вернуло задачу в служебную группу")

    def test_regroup_keeps_the_card_intact(self):
        """PATCH с лишним полем стирает состояние: взятая в работу задача обязана
        остаться взятой, с колонкой, тегом держателя и планом в заметке."""
        out = self.cli("groups", "--create", "zz-секция сохранности").stdout
        tid = self.make_task("zz: перенос не ломает карточку")
        self.cli("start", tid, "--plan", "проверяю перенос в секцию")
        self.cli("regroup", tid, "zz-секция сохранности")
        self.assertEqual(self.group_of(tid), out.rsplit("->", 1)[1].strip())
        self.assertEqual(self.column_of(tid), "wip", "карточка уехала по доске")
        self.assertEqual(self.agent_tags(tid), [f"agent:{AGENT}"])
        self.assertIn(f"ПЛАН (agent:{AGENT})", self.note_of(tid))

    def test_unknown_section_is_refused_without_touching_the_task(self):
        tid = self.make_task("zz: перенос в несуществующую секцию")
        was = self.group_of(tid)
        p = self.cli("regroup", tid, "zz-такой секции нет", expect=1)
        self.assertIn("не найдена", p.stderr)
        self.assertNotIn("HTTP 400", p.stderr, "наружу утёк ответ API")
        self.assertEqual(self.group_of(tid), was, "отказ всё-таки что-то записал")


class CycleTest(LiveBase):

    def test_full_cycle_start_report_done(self):
        tid = self.make_task("zz: полный цикл")

        self.cli("start", tid, "--plan", "шаг раз\nшаг два")
        self.assertEqual(self.column_of(tid), "wip")
        self.assertEqual(self.agent_tags(tid), [f"agent:{AGENT}"])
        note = self.note_of(tid)
        self.assertIn(f"ПЛАН (agent:{AGENT})", note)
        self.assertIn("шаг раз", note)

        # держатель обязан быть виден на доске, а не только в карточке
        board = self.cli("board").stdout
        self.assertIn(tid, board)
        self.assertIn(f"@{AGENT}", board)

        self.cli("report", tid, "промежуточный факт")
        self.assertIn("промежуточный факт", self.note_of(tid))

        self.cli("done", tid, "--report", "готово, проверено")
        self.assertEqual(self.column_of(tid), "done")
        self.assertEqual(self.checked(tid), 1)
        self.assertIn(f"РЕЗУЛЬТАТ (agent:{AGENT})", self.note_of(tid))

    def test_start_demands_a_plan(self):
        tid = self.make_task("zz: старт без плана")
        p = self.cli("start", tid, expect=1)
        self.assertIn("нужен план", p.stderr)
        self.assertEqual(self.column_of(tid), "todo", "колонка сдвинулась при отказе")

    def test_done_refuses_a_task_that_was_never_started(self):
        """Обязательность плана держалась только со стороны `start`, и обойти её
        было штатным путём: `done` по задаче из очереди."""
        tid = self.make_task("zz: закрыть не начиная")
        p = self.cli("done", tid, "--report", "как будто сделано", expect=1)
        self.assertIn("не бралась в работу", p.stderr)
        self.assertEqual(self.column_of(tid), "todo")
        self.assertEqual(self.checked(tid), 0)

    def test_second_agent_cannot_take_a_held_task(self):
        """Две сессии над одной задачей — потерянный контекст, а не параллельность."""
        tid = self.make_task("zz: занята другим")
        self.cli("start", tid, "--plan", "первый агент")
        p = self.cli("start", tid, "--plan", "второй агент",
                     agent=OTHER_AGENT, expect=1)
        self.assertIn("уже занята", p.stderr)
        self.assertEqual(self.agent_tags(tid), [f"agent:{AGENT}"],
                         "чужая метка появилась на занятой задаче")

    def test_take_over_moves_the_holder(self):
        """Перехват занятой задачи. Жёсткий assert — T-fc3a3096 закрыта.

        Раньше здесь было три попытки с предупреждением: `--take-over` делал два
        PATCH подряд и судил о результате по немедленному GET, а тот на
        загруженной очереди синхронизации отдаёт ещё старое состояние. Теперь
        перехват — ОДИН PATCH (`set_task_tags`), а подтверждение перечитывается с
        паузой, так что лаг сервера этот путь больше не роняет, и терпимость к
        нему стала бы прикрытием настоящей регрессии.

        ⚠ Живой прогон сам по себе дефект не ловит: на спокойной очереди зеленел
        и старый код. Нагрузку воспроизводит `tests/test_claim.py` на заглушке —
        там же лежит команда контрольного красного прогона.
        """
        tid = self.make_task("zz: перехват")
        self.cli("start", tid, "--plan", "первый агент")
        self.cli("start", tid, "--plan", "перехватываю", "--take-over",
                 agent=OTHER_AGENT)
        self.assertEqual(self.agent_tags(tid), [f"agent:{OTHER_AGENT}"])


class ReturnPathTest(LiveBase):

    def test_release_returns_to_queue_and_drops_the_tag(self):
        """Частичный откат оставляет задачу, которая выглядит занятой, хотя ею
        никто не занят."""
        tid = self.make_task("zz: возврат в очередь")
        self.cli("start", tid, "--plan", "начал и передумал")
        self.cli("release", tid, "--report", "не воспроизводится")
        self.assertEqual(self.column_of(tid), "todo")
        self.assertEqual(self.agent_tags(tid), [])
        self.assertEqual(self.checked(tid), 0)
        self.assertIn("ВОЗВРАТ В ОЧЕРЕДЬ", self.note_of(tid))

    def test_block_records_the_reason(self):
        tid = self.make_task("zz: блокер")
        self.cli("block", tid, "нет доступа к стенду")
        self.assertEqual(self.column_of(tid), "blocked")
        self.assertIn("БЛОКЕР", self.note_of(tid))
        self.assertIn("нет доступа к стенду", self.note_of(tid))
        # тег вешается и здесь: иначе не видно, кто упёрся
        self.assertEqual(self.agent_tags(tid), [f"agent:{AGENT}"])

    def test_done_review_does_not_tick_the_task(self):
        tid = self.make_task("zz: на проверку")
        self.cli("start", tid, "--plan", "сделаю и покажу")
        self.cli("done", tid, "--report", "нужен взгляд человека", "--review")
        self.assertEqual(self.column_of(tid), "review")
        self.assertEqual(self.checked(tid), 0, "review не должен закрывать задачу")
        self.assertIn("НА ПРОВЕРКУ", self.note_of(tid))


class BoardRepairTest(LiveBase):

    def test_task_without_a_column_is_shown_in_todo_and_move_binds_it(self):
        """Задача без связки — норма, а не поломка: так приходит всё, что человек
        завёл в приложении, и приложение показывает её в «Новые». Доска обязана
        показывать то же; `move` при этом создаёт настоящую связку."""
        tid = self.make_task("zz: без связки с колонкой", column=None)
        self.assertIsNone(self.sing.task_column(tid), "связки быть не должно")
        board = self.cli("board").stdout
        self.assertIn(tid, board, "задача без связки пропала с доски")
        self.assertNotIn("ВНЕ КОЛОНОК", board,
                         "задача из приложения — не сирота, пугать поломкой нечем")
        self.assertIn(tid, self.cli("list").stdout,
                      "очередь обязана видеть задачу из приложения")
        self.cli("move", tid, "todo")
        self.assertEqual(self.column_of(tid), "todo")
        self.assertIsNotNone(self.sing.task_column(tid), "move обязан создать связку")

    def test_move_between_system_columns_is_verified_by_fact(self):
        """`change-column` на СИСТЕМНЫХ колонках отвечает 200, ничего не сделав.
        Прогонять надо ту конфигурацию, что в бою: todo/wip/done здесь системные."""
        tid = self.make_task("zz: системные колонки")
        for role in ("wip", "done", "todo"):
            self.cli("move", tid, role)
            self.assertEqual(self.column_of(tid), role,
                             f"перенос в {role} не применился, а команда не упала")

    def test_rm_removes_the_task_for_real(self):
        tid = self.make_task("zz: уборка за собой")
        self.cli("rm", tid, expect=1)                     # без --yes не удаляет
        self.assertIsNotNone(self.sing.request("GET", f"/task/{tid}", soft=True))
        self.cli("rm", tid, "--yes")
        self.assertIsNone(self.sing.request("GET", f"/task/{tid}", soft=True))


class NotesTest(LiveBase):
    """Круг заметки целиком: создана → дописана → перечитана → удалена. Это и есть
    критерий готовности карточки T-ac96c736 — до правок из CLI жил только первый шаг.

    Один тест на весь круг намеренно: каждая команда здесь — запросы к живому
    трекеру, а прогоны подряд он не держит (429)."""

    def test_note_is_created_appended_reread_and_removed(self):
        out = self.cli("notes", "--add", "zz: контекст проекта",
                       "--text", "прод разворачивается через ansible").stdout
        nid = out.split(":")[0].strip()
        self.assertTrue(nid.startswith("T-"), out)
        note = self.sing.request("GET", f"/task/{nid}")
        self.assertTrue(note.get("isNote"), "создана задача, а не заметка")
        self.assertIn("ansible", self.note_of(nid))
        # заметке не место в очереди задач: канбан её не видит
        self.assertNotIn(nid, self.cli("list").stdout)
        self.assertNotIn(nid, self.cli("board").stdout)

        self.cli("notes", "--edit", nid, "--append", "--text", "с 2026-09 — kubernetes")
        text = self.note_of(nid)
        self.assertIn("ansible", text, "дописывание затёрло прежний текст")
        self.assertIn("kubernetes", text)

        shown = self.cli("notes", "--show", nid).stdout
        self.assertIn("kubernetes", shown)
        self.assertIn("zz: контекст проекта", shown)

        # та же правка второй раз: сервер ответит 200, поэтому команда обязана
        # отказать сама, а не отчитаться успехом
        p = self.cli("notes", "--edit", nid, "--text", text.rstrip("\n"), expect=1)
        self.assertIn("текст тот же", p.stderr)

        self.cli("notes", "--rm", nid, expect=1)              # без --yes не удаляет
        self.assertIsNotNone(self.sing.request("GET", f"/task/{nid}", soft=True))
        self.cli("notes", "--rm", nid, "--yes")
        self.assertIsNone(self.sing.request("GET", f"/task/{nid}", soft=True),
                          "заметка осталась, а команда отчиталась успехом")

    def test_notes_refuse_a_task_that_is_not_a_note(self):
        tid = self.make_task("zz: обычная задача, не заметка")
        p = self.cli("notes", "--edit", tid, "--text", "текст", expect=1)
        self.assertIn("не заметка", p.stderr)
        self.assertEqual(self.note_of(tid), "", "отказ всё-таки что-то записал")


class ScopeGuardTest(LiveBase):
    """Ограничение области проверяется по факту на каждой операции, а не один раз
    при привязке. Здесь дёшево: список проектов уже в памятке."""

    def test_root_project_itself_is_not_workable(self):
        root = self.sing.resolve_root()
        with self.assertRaises(SystemExit):
            self.sing.assert_allowed(root["id"])

    def test_unknown_project_is_refused(self):
        with self.assertRaises(SystemExit):
            self.sing.assert_allowed("P-00000000-0000-0000-0000-000000000000")

    def test_task_from_another_project_is_refused(self):
        """`report T-<чужая>` не должен уходить мимо ограничения: проверяется
        проект САМОЙ задачи, а не только привязка репозитория."""
        tid = self.make_task("zz: чужая для другого репо")
        with self.assertRaises(SystemExit):
            self.sing.assert_task_allowed(tid, {"projectId": "P-someone-else"})

    def test_init_refuses_a_project_outside_the_root(self):
        p = self.cli("init", "--project", self.sing.ROOT_PROJECT_TITLE, expect=1)
        self.assertIn("корневой проект", p.stderr.lower())


if __name__ == "__main__":
    unittest.main()
