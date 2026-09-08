#!/usr/bin/env bash
# Раскатка эталона скилла из этого репозитория во все агентские окружения.
#
#   tools/install.sh                  — сверить и раскатать во все доступные цели
#   tools/install.sh --check          — только сверить; код 1, если есть расхождение
#   tools/install.sh --list           — куда раскатывается и что установлено сейчас
#   tools/install.sh --target codex   — только одна цель
#
# Источник правды — этот репозиторий. Каталоги целей перезаписываются целиком:
# правки, сделанные прямо в них, теряются. В этом и смысл сверки.
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
BACKUP_ROOT="${SINGULARITY_BACKUP_ROOT:-$HOME/.singularity-tasks-backup}"
ITEMS=(SKILL.md README.md scripts references)
# мусор компиляции не является частью скилла и не должен считаться расхождением
DIFF_EXCL=(-x __pycache__ -x "*.pyc")

# имя|признак установленного инструмента|куда класть скилл
# bash 3.2 (штатный на macOS) не умеет ассоциативные массивы — держим строкой
TARGETS="claude|$HOME/.claude|$HOME/.claude/skills/singularity-tasks
codex|$HOME/.codex|$HOME/.codex/skills/singularity-tasks
opencode|$HOME/.config/opencode|$HOME/.config/opencode/skills/singularity-tasks
antigravity|$HOME/.gemini/config|$HOME/.gemini/config/skills/singularity-tasks
qwen|$HOME/.qwen|$HOME/.qwen/skills/singularity-tasks"

mode="install"
only=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)  mode="check" ;;
        --list)   mode="list" ;;
        --target) only="${2:-}"; shift ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
    shift
done

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
installed=0

while IFS='|' read -r name probe dst; do
    [[ -z "$name" ]] && continue
    [[ -n "$only" && "$only" != "$name" ]] && continue
    any=1

    if [[ ! -d "$probe" ]]; then
        echo "· $name — инструмент не установлен ($probe), пропуск"
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
            if target_drift "$dst"; then
                echo "· $name — совпадает"
            else
                echo "· $name — расхождение (см. выше)"
                drift=1
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
            echo
            echo "Расхождение эталона и установленного. Раскатать: tools/install.sh"
            echo "Если правка была сделана в цели — сначала перенести её в репозиторий,"
            echo "иначе установка её затрёт."
            exit 1
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
