"""Подрезка логов упавших прогонов (`run.prune_logs`). Ни сети, ни токена.

Проверяется не «что-то удалилось», а ровно граница: подрезка обязана уносить
старое и обязана НЕ уносить то, ради чего логи и пишутся, — свежую историю и
последние N прогонов метки. Ошибка здесь тихая: каталог гитигнорен, в диффе
ничего не видно, а лог редкого живого падения исчезает молча.

Всё идёт во ВРЕМЕННОМ каталоге: настоящий `tests/logs/` не трогается, иначе
набор сам съедал бы то, что должен беречь.

Запуск: tests/run.py fast
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import support  # noqa: E402

run = support.load_module("run_under_test", os.path.join(HERE, "run.py"))

DAY = 86400
REAL_LOG_DIR = os.path.join(HERE, "logs")


class PruneTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selftest-logs-")
        self.saved_dir = run.LOG_DIR
        run.LOG_DIR = self.tmp
        # гейт на саму проверку: промах подмены означал бы, что мы режем живой каталог
        self.assertNotEqual(os.path.realpath(run.LOG_DIR),
                            os.path.realpath(REAL_LOG_DIR))
        self.now = time.time()

    def tearDown(self):
        run.LOG_DIR = self.saved_dir
        for n in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, n))
        os.rmdir(self.tmp)

    def make(self, label, age_days, tag=""):
        """Лог метки `label` возрастом `age_days` дней."""
        name = f"{label}-{tag or int(age_days * 1000)}.log"
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        mtime = self.now - age_days * DAY
        os.utime(path, (mtime, mtime))
        return name

    def left(self):
        return sorted(os.listdir(self.tmp))

    # ------------------------------------------------------------ обе границы

    def test_old_and_beyond_keep_last_is_removed(self):
        """Старое и вне последних N — уходит, остальное на месте."""
        fresh = [self.make("fast", i * 0.1, f"fresh{i}") for i in range(5)]
        old = [self.make("fast", 30 + i, f"old{i}") for i in range(3)]
        removed = run.prune_logs("fast", now=self.now)
        self.assertEqual(sorted(os.path.basename(p) for p in removed), sorted(old))
        self.assertEqual(self.left(), sorted(fresh))

    def test_keep_last_survives_any_age(self):
        """Последние N не трогаем, даже если им сто лет.

        Это и есть защита редкого свидетельства: набор давно не гоняли — лог
        того единственного падения обязан дожить.
        """
        names = [self.make("fast", 100 + i, f"ancient{i}") for i in range(5)]
        self.assertEqual(run.prune_logs("fast", now=self.now), [])
        self.assertEqual(self.left(), sorted(names))

    def test_fresh_survives_beyond_keep_last(self):
        """Свежее не уносим, сколько бы его ни было: `keep_last` — пол, не потолок."""
        names = [self.make("fast", 1 + i * 0.01, f"today{i}") for i in range(12)]
        self.assertEqual(run.prune_logs("fast", now=self.now), [])
        self.assertEqual(self.left(), sorted(names))

    def test_boundary_day_is_kept(self):
        """Ровно на пороге — ещё не старое (строгое сравнение с обеих сторон)."""
        [self.make("fast", 0.1, f"fresh{i}") for i in range(5)]
        edge = self.make("fast", 7 - 0.01, "edge")       # моложе 7 дней на 15 минут
        past = self.make("fast", 7 + 0.01, "past")       # старше 7 дней на 15 минут
        removed = run.prune_logs("fast", now=self.now)
        self.assertEqual([os.path.basename(p) for p in removed], [past])
        self.assertIn(edge, self.left())

    # -------------------------------------------------------- метки раздельно

    def test_labels_do_not_touch_each_other(self):
        """Подрезка `fast` не уносит логи `live` — у них разная цена и политика."""
        live_old = [self.make("live", 200 + i, f"liveold{i}") for i in range(25)]
        self.make("fast", 30, "fastold")
        run.prune_logs("fast", now=self.now)
        for n in live_old:
            self.assertIn(n, self.left())

    def test_live_threshold_is_looser_than_fast(self):
        """Одинаковый набор файлов: у `fast` он подрезается, у `live` — нет.

        Живой прогон стоит ~4 мин и трогает трекер; его история дороже.
        """
        for i in range(10):
            self.make("fast", 30 + i, f"f{i}")
            self.make("live", 30 + i, f"l{i}")
        self.assertEqual(len(run.prune_logs("fast", now=self.now)), 5)
        self.assertEqual(run.prune_logs("live", now=self.now), [])

    def test_unknown_label_is_left_alone(self):
        """Метка без политики (ручной файл, будущая группа) не подрезается."""
        names = [self.make("slow", 500 + i, f"s{i}") for i in range(9)]
        self.assertEqual(run.prune_logs("slow", now=self.now), [])
        self.assertEqual(self.left(), sorted(names))

    def test_missing_dir_is_not_an_error(self):
        """Первый прогон на чистой машине: каталога ещё нет."""
        run.LOG_DIR = os.path.join(self.tmp, "нет-такого")
        self.assertEqual(run.prune_logs("fast", now=self.now), [])
        run.LOG_DIR = self.tmp

    # ---------------------------------------------------- подрезка подключена

    def test_save_failure_log_actually_prunes(self):
        """Гейт против «написано, но не вызывается»: режет сам путь сохранения."""
        old = [self.make("fast", 30 + i, f"old{i}") for i in range(3)]
        fresh = [self.make("fast", 0.1 * i, f"fresh{i}") for i in range(4)]
        path, pruned = run.save_failure_log("fast", "падение")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(sorted(os.path.basename(p) for p in pruned), sorted(old))
        left = self.left()
        self.assertEqual(len(left), 5)                 # 4 свежих + только что записанный
        for n in fresh:
            self.assertIn(n, left)


if __name__ == "__main__":
    unittest.main()
