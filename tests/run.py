#!/usr/bin/env python3
"""Регрессионный набор скилла singularity-tasks. Одна команда.

Группы разделены по СТОИМОСТИ, а не по слоям — чтобы дешёвое можно было гонять
после каждой правки, а дорогое осознанно:

    tests/run.py            # то же, что fast
    tests/run.py fast       # без сети и БЕЗ ТОКЕНА: чистые функции + заглушка HTTP
    tests/run.py live       # живой трекер, черновой подпроект zz-*, уборка за собой
    tests/run.py slow       # tools/check-retry.py целиком: боевые константы, ~50 с
    tests/run.py all        # fast + live
    tests/run.py fast -v    # подробный вывод unittest

    tests/run.py --drafts   # какие черновики zz-* живы прямо сейчас
    tests/run.py --sweep    # снести черновики ЭТОГО набора (zz-selftest-*)

`fast` обязана проходить в чужом окружении, где Keychain пуст: ни один быстрый
тест не читает токен.

`--sweep` намеренно трогает только `zz-selftest-*`, а не всю маску `zz-*`:
рядом живут черновики других сессий, и подметать их — значит убить чужую работу.
"""

import contextlib
import datetime
import io
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import support  # noqa: E402

GROUPS = {"fast": ["test_pure", "test_retry", "test_claim"], "live": ["test_live"]}
SWEEP_PREFIX = "zz-selftest-"
LOG_DIR = os.path.join(HERE, "logs")


class _Tee:
    """Пишет в оба потока: прогон видно как обычно, и он же копится для файла."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
        return len(text)

    def flush(self):
        for s in self.streams:
            s.flush()


def save_failure_log(label, text):
    """Сохранить вывод упавшего прогона.

    Падение, не оставившее следа, равносильно отсутствию проверки: «прогони ещё
    раз» становится способом не заметить дефект. Живой набор работает с трекером,
    где есть очередь синхронизации, и краснеет не каждый раз — поймать такое
    можно только по сохранённому логу.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(LOG_DIR, f"{label}-{stamp}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def run_group(names, verbosity, label=None):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(n) for n in names)
    buf = io.StringIO()
    # unittest пишет в stderr, тесты печатают в stdout — копим оба, иначе в логе
    # окажется половина картины
    with contextlib.redirect_stdout(_Tee(sys.stdout, buf)):
        result = unittest.TextTestRunner(stream=_Tee(sys.stderr, buf),
                                         verbosity=verbosity).run(suite)
    if not result.wasSuccessful() and label:
        path = save_failure_log(label, buf.getvalue())
        print(f"\nвывод упавшего прогона сохранён: {path}", file=sys.stderr)
    return result.wasSuccessful()


def run_slow():
    """Прежняя проверка повторов целиком — не переписана, а вызвана как есть."""
    print("tools/check-retry.py: 19 сценариев на боевых константах (~50 с)\n")
    return subprocess.call([sys.executable,
                            os.path.join(REPO, "tools", "check-retry.py")]) == 0


def drafts():
    sing = support.load_sing("sing_sweep")
    zz = support.load_tool("zz-project.py")
    return sing, zz, zz.drafts(sing)


def cmd_drafts():
    _, _, left = drafts()
    for p in left:
        mine = "  ← от этого набора" if p["title"].startswith(SWEEP_PREFIX) else ""
        print(f"{p['id']}  {p['title']}{mine}")
    print(f"черновиков zz-*: {len(left)}")
    return True


def cmd_sweep():
    sing, zz, left = drafts()
    mine = [p for p in left if p["title"].startswith(SWEEP_PREFIX)]
    if not mine:
        print(f"черновиков {SWEEP_PREFIX}* не осталось "
              f"(всего zz-*: {len(left)}, они чужие — не трогаю)")
        return True
    ok = True
    for p in mine:
        try:
            hit, rest = zz.delete_draft(sing, p["id"])
            print(f"удалён {hit['id']}  {hit['title']}")
        except Exception as e:                       # noqa: BLE001 — доложить и идти дальше
            ok = False
            print(f"НЕ удалён {p['id']}  {p['title']}: {e}", file=sys.stderr)
    return ok


def main():
    argv = sys.argv[1:]
    verbosity = 2 if ("-v" in argv or "--verbose" in argv) else 1
    argv = [a for a in argv if a not in ("-v", "--verbose")]

    if "--drafts" in argv:
        return 0 if cmd_drafts() else 1
    if "--sweep" in argv:
        return 0 if cmd_sweep() else 1

    group = argv[0] if argv else "fast"
    if group not in ("fast", "live", "slow", "all"):
        print(__doc__)
        return 2

    ok = True
    if group in ("fast", "all"):
        print("=== fast: без сети и без токена ===")
        ok &= run_group(GROUPS["fast"], verbosity, label="fast")
    if group == "slow":
        ok &= run_slow()
    if group in ("live", "all"):
        print("\n=== live: живой трекер, черновой подпроект zz- ===")
        try:
            ok &= run_group(GROUPS["live"], verbosity, label="live")
        finally:
            # Страховка поверх tearDownModule: если прогон убили посреди уборки,
            # черновик этого набора не должен пережить команду.
            if not cmd_sweep():
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
