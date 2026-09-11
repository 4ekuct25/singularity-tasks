#!/usr/bin/env python3
"""Счётчик HTTP-запросов и времени для любой команды scripts/sing.py.

Нужен, чтобы «цена команды» была числом, а не ощущением: правки board/list
добавляют запросы незаметно, а на живом API каждый лишний запрос виден.

Считает ровно то, что уходит в сеть: перехватывается urlopen внутри sing.py,
а не вызовы его же функции request() — paged() под капотом делает несколько
HTTP-вызовов на один request-путь, и счёт по request() занизил бы цену.

    tools/count-requests.py board
    tools/count-requests.py --repeat 3 board --limit 10
    tools/count-requests.py --repeat 3 list --column wip

Сравнение «до/после» — через --script на копию прежней версии, чтобы можно было
чередовать A/B/A в одном заходе: разброс сети между двумя окнами больше разницы
между вариантами, и два прогона подряд ничего не доказывают.

    git show HEAD:scripts/sing.py > /tmp/sing-before.py
    tools/count-requests.py --script /tmp/sing-before.py --repeat 3 board
"""

import argparse
import collections
import importlib.util
import io
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SING = os.path.join(os.path.dirname(HERE), "scripts", "sing.py")


def load_sing(path=None):
    spec = importlib.util.spec_from_file_location("sing", path or SING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_once(sing, argv, quiet=True):
    """Один прогон команды. Возвращает (секунды, счётчик путей, вывод)."""
    calls = collections.Counter()
    real_urlopen = urllib.request.urlopen

    def counting_urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        path = urllib.parse.urlparse(url).path
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        calls[f"{method} {path}"] += 1
        return real_urlopen(req, *a, **kw)

    urllib.request.urlopen = counting_urlopen
    buf = io.StringIO()
    old_stdout, old_argv = sys.stdout, sys.argv
    if quiet:
        sys.stdout = buf
    sys.argv = ["sing.py"] + argv
    t0 = time.perf_counter()
    try:
        sing.main()
    except SystemExit as e:
        if e.code not in (None, 0, 2):
            raise
    finally:
        elapsed = time.perf_counter() - t0
        sys.stdout, sys.argv = old_stdout, old_argv
        urllib.request.urlopen = real_urlopen
    return elapsed, calls, buf.getvalue()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repeat", type=int, default=1,
                   help="прогонов (разброс сети больше разницы правок — нужен повтор)")
    p.add_argument("--show", action="store_true", help="напечатать вывод последнего прогона")
    p.add_argument("--script", help="какой sing.py мерить (по умолчанию — из этого репо)")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if not args.argv:
        p.error("нужна команда sing.py, например: board")

    sing = load_sing(args.script)
    times, calls, out = [], None, ""
    for i in range(args.repeat):
        elapsed, calls, out = run_once(sing, args.argv)
        times.append(elapsed)
        print(f"прогон {i + 1}: {elapsed:.2f} c, запросов {sum(calls.values())}",
              file=sys.stderr)

    print(f"\nкоманда: sing.py {' '.join(args.argv)}")
    print(f"запросов всего: {sum(calls.values())}")
    for path, n in sorted(calls.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}  {path}")
    print(f"время, c: мин {min(times):.2f} / медиана "
          f"{sorted(times)[len(times) // 2]:.2f} / макс {max(times):.2f}"
          f"  (прогонов {len(times)})")
    if args.show:
        print("\n--- вывод ---")
        print(out)


if __name__ == "__main__":
    main()
