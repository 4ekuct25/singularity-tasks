#!/usr/bin/env bash
# Раскатка эталона скилла из этого репозитория во все агентские окружения.
#
#   tools/install.sh                  — сверить и раскатать во все доступные цели
#   tools/install.sh --check          — только сверить; код 1, если есть расхождение
#   tools/install.sh --check --quiet  — то же без подробностей: список разошедшихся
#                                       целей одной строкой, при совпадении — молчок
#   tools/install.sh --list           — куда раскатывается и что установлено сейчас
#   tools/install.sh --target codex   — только одна цель
#   tools/install.sh --force          — раскатать из git worktree (по умолчанию отказ)
#
# Источник правды — этот репозиторий. Каталоги целей перезаписываются целиком:
# правки, сделанные прямо в них, теряются. В этом и смысл сверки.
#
# `--check --quiet` — общий примитив сверки: им же пользуется scripts/sing.py,
# чтобы предупредить о расхождении в момент start/done. Логика сверки должна
# оставаться в одном месте, иначе два «одинаковых» ответа разъедутся.
#
# Формат SKILL.md общий для всех пяти инструментов, различаются только пути.
# ВАЖНО, эти каталоги легко перепутать:
#   ~/.gemini/skills         — Gemini CLI (не Antigravity!)
#   ~/.gemini/config/skills  — Antigravity
#   ~/.qwen/skills           — Qwen Code
# Qwen сканирует ещё и ~/.agents/skills, но ставим только в один каталог: иначе он
# прочитает скилл дважды и получит два одинаковых описания с теми же триггерами.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ITEMS=(SKILL.md README.md scripts references)
# мусор компиляции не является частью скилла и не должен считаться расхождением
DIFF_EXCL=(-x __pycache__ -x "*.pyc")

# Корень, от которого считаются каталоги инструментов. Переопределяется только для
# проверки самого установщика в песочнице: иначе единственный способ убедиться, что
# сверка молчит при совпадении, — сначала испортить живые каталоги пяти агентов.
# В бою переменная не задаётся, целями остаются каталоги пользователя.
TARGET_HOME="${SINGULARITY_TARGET_HOME:-$HOME}"
# Бэкап по умолчанию считается от того же корня, что и цели: прогон в песочнице
# иначе затрёт настоящие бэкапы копией из песочницы — то есть сломает путь отката
# ровно тем действием, которое затевалось, чтобы ничего не сломать.
BACKUP_ROOT="${SINGULARITY_BACKUP_ROOT:-$TARGET_HOME/.singularity-tasks-backup}"

# имя|признак установленного инструмента|куда класть скилл
# bash 3.2 (штатный на macOS) не умеет ассоциативные массивы — держим строкой
TARGETS="claude|$TARGET_HOME/.claude|$TARGET_HOME/.claude/skills/singularity-tasks
codex|$TARGET_HOME/.codex|$TARGET_HOME/.codex/skills/singularity-tasks
opencode|$TARGET_HOME/.config/opencode|$TARGET_HOME/.config/opencode/skills/singularity-tasks
antigravity|$TARGET_HOME/.gemini/config|$TARGET_HOME/.gemini/config/skills/singularity-tasks
qwen|$TARGET_HOME/.qwen|$TARGET_HOME/.qwen/skills/singularity-tasks"

mode="install"
only=""
quiet=""
force=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)  mode="check" ;;
        --list)   mode="list" ;;
        --quiet)  quiet="quiet" ;;
        --force)  force=1 ;;
        --target) only="${2:-}"; shift ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
    shift
done

# Раскатка из git worktree затрёт общие каталоги пяти инструментов содержимым
# отдельной ветки — вместе с незакоммиченной работой того, кто в этом worktree
# сидит, и поверх работы параллельных сессий. Сверять из worktree можно и нужно,
# раскатывать — только осознанно.
if [[ "$mode" == "install" && $force -eq 0 && -f "$SRC/.git" ]]; then
    cat >&2 <<EOF
Отказ: это git worktree ($SRC), а не основной рабочий каталог репозитория.
Раскатка отсюда зальёт в общие каталоги пяти инструментов содержимое этой ветки,
включая незакоммиченное, и затрёт работу параллельных сессий.
Раскатывать после слияния из основного каталога. Сверить отсюда можно:
    tools/install.sh --check
Если это осознанное решение — tools/install.sh --force
EOF
    exit 2
fi

# --------------------------------------------------------------------- вспомогательное

target_drift() {   # 0 — совпадает, 1 — расходится; печатает подробности
    local dst="$1" quiet="${2:-}" d=0 item
    for item in "${ITEMS[@]}"; do
        if [[ ! -e "$dst/$item" ]]; then
            [[ -n "$quiet" ]] || echo "    ✗ отсутствует: $item"
            d=1
        elif ! diff -r -q "${DIFF_EXCL[@]}" "$SRC/$item" "$dst/$item" >/dev/null 2>&1; then
            [[ -n "$quiet" ]] || {
                echo "    ✗ расходится: $item"
                # diff возвращает 1 при различиях; без подавления set -e и pipefail
                # убивают скрипт ровно здесь — то есть тогда, когда работа нужнее всего
                { diff -r -u "${DIFF_EXCL[@]}" "$SRC/$item" "$dst/$item" 2>&1 || true; } \
                    | sed 's/^/        /' | head -20
            }
            d=1
        fi
    done
    return $d
}

deploy() {
    local name="$1" dst="$2" bak="$BACKUP_ROOT/$name" item
    if [[ -d "$dst" ]]; then
        mkdir -p "$BACKUP_ROOT"
        rm -rf "$bak"
        cp -R "$dst" "$bak"
        echo "    бэкап: $bak"
    fi
    mkdir -p "$dst"
    for item in "${ITEMS[@]}"; do
        rm -rf "${dst:?}/$item"
        cp -R "$SRC/$item" "$dst/$item"
    done
    find "$dst" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    chmod +x "$dst/scripts/sing.py"
    if target_drift "$dst" quiet; then
        echo "    ✓ раскатано"
    else
        echo "    ✗ после установки всё ещё расходится" >&2
        exit 1
    fi
}

# --------------------------------------------------------------------- основной цикл

any=0
drift=0
drift_names=""
installed=0

while IFS='|' read -r name probe dst; do
    [[ -z "$name" ]] && continue
    [[ -n "$only" && "$only" != "$name" ]] && continue
    any=1

    if [[ ! -d "$probe" ]]; then
        # не установленный инструмент — не расхождение: нечему расходиться
        [[ -n "$quiet" ]] || echo "· $name — инструмент не установлен ($probe), пропуск"
        continue
    fi

    case "$mode" in
        list)
            if [[ -d "$dst" ]]; then
                state=$(target_drift "$dst" quiet && echo "совпадает" || echo "РАСХОДИТСЯ")
            else
                state="не установлен"
            fi
            printf "· %-12s %-8s %s\n" "$name" "$state" "$dst"
            ;;
        check)
            if target_drift "$dst" "$quiet"; then
                [[ -n "$quiet" ]] || echo "· $name — совпадает"
            else
                [[ -n "$quiet" ]] || echo "· $name — расхождение (см. выше)"
                drift=1
                drift_names="${drift_names:+$drift_names, }$name"
            fi
            ;;
        install)
            echo "· $name -> $dst"
            if [[ -d "$dst" ]] && target_drift "$dst" quiet; then
                echo "    ✓ уже совпадает"
            else
                deploy "$name" "$dst"
                installed=$((installed + 1))
            fi
            ;;
    esac
done <<< "$TARGETS"

if [[ $any -eq 0 ]]; then
    echo "Нет подходящих целей${only:+ (--target $only)}." >&2
    exit 2
fi

case "$mode" in
    check)
        if [[ $drift -eq 1 ]]; then
            # quiet: только список целей в stdout — его разбирает вызывающий (sing.py).
            # Молчание при совпадении здесь принципиально: гейт, который печатает
            # что-то на каждом запуске, перестают читать.
            if [[ -n "$quiet" ]]; then
                echo "$drift_names"
                exit 1
            fi
            echo
            echo "Расхождение эталона и установленного. Раскатать: tools/install.sh"
            echo "Если правка была сделана в цели — сначала перенести её в репозиторий,"
            echo "иначе установка её затрёт."
            exit 1
        fi
        if [[ -n "$quiet" ]]; then
            exit 0
        fi
        echo
        echo "✓ все цели совпадают с эталоном"
        ;;
    install)
        echo
        if [[ $installed -gt 0 ]]; then
            echo "Обновлено целей: $installed. Бэкапы: $BACKUP_ROOT/<цель>"
            echo "Откат цели: rm -rf <путь-цели> && mv $BACKUP_ROOT/<цель> <путь-цели>"
            echo "Перезапустить сессии агентов, чтобы они перечитали скиллы."
        else
            echo "Всё уже актуально, ничего не менялось."
        fi
        ;;
esac
