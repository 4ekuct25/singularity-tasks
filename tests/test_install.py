#!/usr/bin/env python3
"""tools/install.sh — префлайт «чужая правка в цели» в песочнице.

Весь install.sh копируется во временный каталог БЕЗ git: из настоящего
worktree его перехватил бы отказ от раскатки (он тоже проверяется, но здесь
свой). Запуск — с SINGULARITY_TARGET_HOME и SINGULARITY_BACKUP_ROOT на
временные каталоги, живые каталоги целей и бэкапы не трогаются.

Проверяется ровно то, что раскатка обязана различать ПО ФАЙЛУ:
  * файл в цели, чьё содержимое нет ни в записи последней раскатки, ни в
    эталоне, — называется и блокирует раскатку (код 2), --force пробивает
    с бэкапом правки; отказ выдерживает длинный дифф (SIGPIPE-регрессия);
  * своя НЕРАСКАТАННАЯ правка эталона (файлы цели = записи) — НЕ блокирует,
    в том числе вместе с переносом чужой правки в репозиторий;
  * правку из цели перенесли в репозиторий (файл цели = эталону) — НЕ блокирует,
    запись обновляется, следующая раскатка не лжёт;
  * новый файл в цели и удаление файла, есть в эталоне, — тоже чужие правки;
  * __pycache__ и .DS_Store в цели — не правки;
  * без записи или с битой раскатка идёт с предупреждением, запись (пере)
    пишется, гейт снова вооружён;
  * --check на красной цели называет чужие файлы; контракт --check --quiet
    (одна строка с именами, молчание при совпадении) не меняется.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RECORD = ".singularity-applied"
ITEMS = ("SKILL.md", "README.md", "scripts", "references")
TARGET_NAMES = ("agents", "claude", "antigravity")


class InstallForeignEditTest(unittest.TestCase):
    """Правка в каталоге цели не должна молча уезжать под раскаткой."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sing-install-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox = os.path.join(self.tmp, "sandbox")
        os.makedirs(os.path.join(self.sandbox, "tools"))
        for item in ITEMS:
            src, dst = os.path.join(REPO, item), os.path.join(self.sandbox, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, dst)
        shutil.copy2(os.path.join(REPO, "tools", "install.sh"),
                     os.path.join(self.sandbox, "tools", "install.sh"))
        self.home = os.path.join(self.tmp, "home")
        self.backup = os.path.join(self.tmp, "backup")
        # признаки установленных инструментов: agents — сам home, остальные — каталоги
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.home, ".gemini", "config"))

    # ------------------------------------------------------------- обвязка

    def run_installer(self, *args):
        env = dict(os.environ,
                   SINGULARITY_TARGET_HOME=self.home,
                   SINGULARITY_BACKUP_ROOT=self.backup)
        p = subprocess.run(
            ["bash", os.path.join(self.sandbox, "tools", "install.sh")] + list(args),
            env=env, capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def target(self, name):
        return {
            "agents": os.path.join(self.home, ".agents", "skills", "singularity-tasks"),
            "claude": os.path.join(self.home, ".claude", "skills", "singularity-tasks"),
            "antigravity": os.path.join(self.home, ".gemini", "config", "skills",
                                        "singularity-tasks"),
        }[name]

    def install_once(self):
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 0, "первая раскатка: %s\n%s" % (out, err))

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def append(self, path, line):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    @staticmethod
    def _hash(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def plant(self, name, rel, line):
        """Правка прямо в каталоге цели — то, что делать нельзя, но что делают."""
        path = os.path.join(self.target(name), rel)
        self.append(path, line)
        return path

    def change_standard(self, line):
        """Своя правка эталона, ещё не раскатанная."""
        return self.append(os.path.join(self.sandbox, "SKILL.md"), line)

    # ------------------------------------------------------------- сценарии

    def test_first_install_writes_record(self):
        self.install_once()
        for name in TARGET_NAMES:
            rec_path = os.path.join(self.target(name), RECORD)
            self.assertTrue(os.path.isfile(rec_path), f"записи нет у {name}")
            rec = dict(l.rstrip("\n").split("=", 1)
                       for l in self.read(rec_path).splitlines() if "=" in l)
            self.assertEqual(rec["SKILL.md"],
                             self._hash(os.path.join(self.target(name), "SKILL.md")))
            self.assertEqual(rec["scripts/sing.py"],
                             self._hash(os.path.join(self.target(name), "scripts", "sing.py")))
            self.assertIn("references/api.md", rec)

    def test_planted_foreign_edit_refuses_and_names_file(self):
        """Подложенная правка в цель: раскатка отказывается и называет файл."""
        self.install_once()
        path = self.plant("agents", "scripts/sing.py", "# чужая правка\n")
        self.change_standard("# своя правка\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("Отказ", out + err)
        self.assertIn("agents", out + err)
        self.assertIn("scripts/sing.py", out)
        # ничего не затёрто: правка в цели цела
        self.assertIn("# чужая правка", self.read(path))

    def test_refusal_is_all_or_nothing(self):
        """Отказ — до какой-либо раскатки: ни одна цель не меняется."""
        self.install_once()
        line = "# новая правка эталона\n"
        self.change_standard(line)
        self.plant("claude", "README.md", "# чужая правка\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("claude", out + err)
        for name in TARGET_NAMES:
            with open(os.path.join(self.target(name), "SKILL.md"), encoding="utf-8") as f:
                self.assertNotIn(line.rstrip("\n"), f.read(),
                                 f"{name}: раскатка прошла вопреки отказу")

    def test_planted_edit_in_two_targets_names_both(self):
        self.install_once()
        self.plant("agents", "SKILL.md", "# чужая A\n")
        self.plant("antigravity", "SKILL.md", "# чужая B\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 2, out + err)
        # подробности (список файлов) — по КАЖДОЙ чужой цели, а не только по первой
        self.assertIn("· agents — в цели правки", out)
        self.assertIn("· antigravity — в цели правки", out)
        self.assertIn("SKILL.md", out)

    def test_large_diff_still_refuses_with_message(self):
        """Дифф длиннее 30 строк: head в пайпе ловил SIGPIPE (141) и скрипт
        умирал до сообщения об отказе — sed -n должен это снять."""
        self.install_once()
        self.plant("agents", "scripts/sing.py", "# чужая правка\n")
        for i in range(40):   # > 30 строк диффа цель-против-эталона
            self.append(os.path.join(self.sandbox, "SKILL.md"), f"# новая строка {i}\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 2, f"rc={rc}\n{out}\n{err}")
        self.assertIn("Отказ", err)
        self.assertIn("· agents — в цели правки", out)

    def test_own_unpushed_change_rolls_out(self):
        """Цель = запись последней раскатки: это своя нераскатанная правка, НЕ отказ."""
        self.install_once()
        line = "# своя нераскатанная правка\n"
        self.change_standard(line)
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Отказ", out + err)
        for name in TARGET_NAMES:
            self.assertIn(line.rstrip("\n"),
                          self.read(os.path.join(self.target(name), "SKILL.md")))
        # повтор: всё синхронно, без предупреждений
        rc2, out2, err2 = self.run_installer()
        self.assertEqual(rc2, 0, out2 + err2)
        self.assertEqual(out2.count("уже совпадает"), 3, out2)
        self.assertNotIn("проверить нечем", out2 + err2)

    def test_change_moved_into_repo_with_own_change_in_other_file(self):
        """Чужая правка перенесена в репозиторий, а в эталоне ещё и своя
        нераскатанная правка в ДРУГОМ файле — по-файлу всё не чужое, раскатка
        идёт без отказа. Цель-целиком сравнение здесь давало ложный отказ."""
        self.install_once()
        line = "# правка, перенесённая в репо\n"
        self.plant("agents", "SKILL.md", line)
        self.append(os.path.join(self.sandbox, "SKILL.md"), line)
        self.append(os.path.join(self.sandbox, "references", "api.md"),
                    "# своя нераскатанная правка\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Отказ", out + err)
        # запись обновили: дальше раскатка эталона без ложного отказа
        self.change_standard("# ещё одна правка\n")
        rc2, out2, err2 = self.run_installer()
        self.assertEqual(rc2, 0, out2 + err2)
        self.assertNotIn("Отказ", out2 + err2)

    def test_planted_new_file_refuses(self):
        """Файл, которого не было в последней раскатке и нет в эталоне."""
        self.install_once()
        p = os.path.join(self.target("agents"), "scripts", "extra.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# чужой файл\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("scripts/extra.py", out)
        self.assertTrue(os.path.exists(p), "новый файл не должен исчезать до раскатки")

    def test_deleted_file_in_target_refuses(self):
        """Файл удалён в цели, но есть в эталоне — раскатка восстановит его."""
        self.install_once()
        p = os.path.join(self.target("agents"), "references", "api.md")
        os.remove(p)
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("references/api.md", out)
        self.assertIn("удалён в цели", out)

    def test_foreign_edit_with_force_rolls_out_and_keeps_backup(self):
        self.install_once()
        line = "# чужая правка\n"
        path = self.plant("agents", "scripts/sing.py", line)
        rc, out, err = self.run_installer("--force")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(line.rstrip("\n"), self.read(path))
        backed_up = self.read(os.path.join(self.backup, "agents", "scripts", "sing.py"))
        self.assertIn(line.rstrip("\n"), backed_up, "бэкап должен нести правку")

    def test_pycache_and_dsstore_in_target_are_not_foreign_edits(self):
        self.install_once()
        pyc = os.path.join(self.target("agents"), "scripts", "__pycache__")
        os.makedirs(pyc)
        open(os.path.join(pyc, "sing.cpython-311.pyc"), "w").close()
        open(os.path.join(self.target("agents"), "scripts", ".DS_Store"), "wb").close()
        self.change_standard("# новая правка эталона\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Отказ", out + err)

    def test_missing_record_warns_and_rolls_out(self):
        self.install_once()
        os.remove(os.path.join(self.target("agents"), RECORD))
        self.change_standard("# новая правка эталона\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("проверить нечем", out + err)
        self.assertTrue(os.path.exists(os.path.join(self.target("agents"), RECORD)),
                        "раскатка обязана написать запись")

    def test_corrupted_record_warns_rolls_out_and_rearms_gate(self):
        self.install_once()
        rec = os.path.join(self.target("agents"), RECORD)
        with open(rec, "w", encoding="utf-8") as f:
            f.write("битая запись без хешей\n")
        self.change_standard("# новая правка эталона\n")
        rc, out, err = self.run_installer()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("проверить нечем", out + err)
        # гейт снова вооружён: подложенная правка теперь отказ
        self.plant("agents", "SKILL.md", "# чужая правка\n")
        self.change_standard("# ещё правка\n")
        rc2, out2, err2 = self.run_installer()
        self.assertEqual(rc2, 2, out2 + err2)

    def test_check_names_the_planted_edit_and_quiet_contract_holds(self):
        self.install_once()
        rc, out, err = self.run_installer("--check")
        self.assertEqual(rc, 0, out + err)          # до подложки — тихо
        self.plant("agents", "references/api.md", "# чужая правка\n")
        rc, out, err = self.run_installer("--check")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("agents", out)
        self.assertIn(RECORD, out)                  # уточнение: цель тронули после раскатки
        self.assertIn("references/api.md", out)
        rcq, outq, errq = self.run_installer("--check", "--quiet")
        self.assertEqual(rcq, 1, outq + errq)
        self.assertEqual(outq.strip(), "agents", "quiet: только имена, одной строкой")


if __name__ == "__main__":
    unittest.main()
