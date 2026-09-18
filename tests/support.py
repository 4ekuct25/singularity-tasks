"""Общее для набора: загрузка проверяемого кода и чистое окружение.

Только stdlib, как и весь скилл.

Почему модули грузятся через importlib, а не `import`:
  · `scripts/sing.py` — скрипт, а не пакет, и его `API` читается из окружения
    В МОМЕНТ ИМПОРТА. Значит, нацелить его на заглушку можно только выставив
    переменную до `exec_module` — обычный `import` этого не даёт.
  · `tools/*.py` — с дефисом в имени, обычным `import` их не взять вовсе.

⚠️ Ловушка, ради которой здесь снимок окружения. Проверки заглушки выставляют
`SINGULARITY_API=http://127.0.0.1:...`. Если после них живые проверки унаследуют
эту переменную, они молча уйдут в заглушку и позеленеют, ничего не проверив —
самый дорогой вид ложного «ок». Поэтому оригинальное окружение снимается ОДИН
раз при импорте (до того, как тесты успели что-то поменять), а живые проверки
собирают окружение подпроцессов только из него.
"""

import contextlib
import datetime
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SING = os.path.join(REPO, "scripts", "sing.py")
TOOLS = os.path.join(REPO, "tools")

# Снимок делается при импорте support — раньше любого теста.
_TRACKED = ("SINGULARITY_API", "SINGULARITY_TOKEN", "SINGULARITY_AGENT")
ORIGINAL_ENV = {k: os.environ.get(k) for k in _TRACKED}


@contextlib.contextmanager
def env(**overrides):
    """Временно подменить переменные окружения и вернуть как было."""
    saved = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def clean_env(**overrides):
    """Окружение для подпроцесса: как на старте набора, плюс явные правки.

    Именно clean_env, а не dict(os.environ): см. предупреждение в docstring модуля.
    """
    e = dict(os.environ)
    for k, v in ORIGINAL_ENV.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    for k, v in overrides.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


def utc_of_local(day, hour=0, minute=0):
    """Локальные дата и время этой машины -> строка UTC-ISO, как их хранит трекер.

    Так приложение и пишет выбранный человеком ДЕНЬ: замер по живой базе
    (19.09.2026) — 970 задач со `start` ровно `21:00:00Z`, то есть полночь по
    Москве (+03). Экземпляры повторяющихся серий приходят в этой же форме.

    ⚠️ Проверки «какой это календарный день» обязаны строиться отсюда, а не из
    написанного руками `…T00:00:00.000Z`. Такая строка означает РАЗНЫЙ день в
    разных зонах: набор зеленел бы или краснел от зоны машины, а не от поведения
    скилла, — и ровно так две проверки про `start` молча закрепляли UTC-срез
    (T-d4d2eac7).
    """
    naive = datetime.datetime.combine(day, datetime.time(hour, minute))
    return (naive.astimezone(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000Z"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_sing(name="sing_under_test", api=None, token=None):
    """Загрузить scripts/sing.py. api/token действуют только на время импорта."""
    with env(SINGULARITY_API=api, SINGULARITY_TOKEN=token) if (api or token) \
            else contextlib.nullcontext():
        return load_module(name, SING)


def load_tool(filename, name=None):
    """Взять инструмент из tools/ как модуль (в именах дефисы — только по пути)."""
    if name is None:
        name = filename[:-3].replace("-", "_") if filename.endswith(".py") \
            else filename.replace("-", "_")
    return load_module(name, os.path.join(TOOLS, filename))
