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
#                                       и пропустить отказ префлайта на чужую правку в цели
#
# Источник правды — этот репозиторий. Каталоги целей перезаписываются целиком:
# правки, сделанные прямо в них, теряются. В этом и смысл сверки.
#
# Перед раскаткой каждая цель сверяется с записью последней раскатки
# (.singularity-applied в каталоге цели; запись пишет сама раскатка, не руками).
# Цель, которая расходится И с записью, И с эталоном (рабочим деревом), несёт
# правку, которой нет ни в одной версии эталона, — раскатка затрёт её, поэтому
# такие цели называются (файлы + дифф) и раскатка отказывается, пока не --force.
# Цель = запись (своя нераскатанная правка эталона) или цель = эталон (правку из
# цели перенесли в репозиторий) — раскатка идёт как обычно, запись обновляется.
#
# `--check --quiet` — общий примитив сверки: им же пользуется scripts/sing.py,
# чтобы предупредить о расхождении в момент start/done. Логика сверки должна
# оставаться в одном месте, иначе два «одинаковых» ответа разъедутся.
#
# Формат SKILL.md общий для всех инструментов, различаются только пути.
# Раскладка (2026-10-04, все скиллы машины сведены в ~/.agents/skills):
#   ~/.agents/skills         — общий: его сами читают Codex, Qwen Code, OpenCode, Gemini CLI
#   ~/.claude/skills         — Claude Code (общий каталог не читает)
#   ~/.gemini/config/skills  — Antigravity (общий каталог не читает; ~/.gemini/skills —
#                              это Gemini CLI, не Antigravity!)
# Свои копии у Codex/Qwen/OpenCode теперь ЛИШНИЕ: инструмент прочитал бы скилл дважды
# (свой каталог + общий) и получил два одинаковых описания с теми же триггерами.
# --check считает такую копию расхождением, раскатка убирает её с бэкапом (LEGACY).
# Какой агент запустил sing.py, скрипт узнаёт по дереву процессов, а не по
# каталогу копии — см. AGENT_BY_PROCESS в scripts/sing.py.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ITEMS=(SKILL.md README.md scripts references)
# мусор компиляции не является частью скилла и не должен считаться расхождением
DIFF_EXCL=(-x __pycache__ -x "*.pyc")

# Корень, от которого считаются каталоги инструментов. Переопределяется только для
# проверки самого установщика в песочнице: иначе единственный способ убедиться, что
# сверка молчит при совпадении, — сначала испортить живые каталоги раскатки.
# В бою переменная не задаётся, целями остаются каталоги пользователя.
TARGET_HOME="${SINGULARITY_TARGET_HOME:-$HOME}"
# Бэкап по умолчанию считается от того же корня, что и цели: прогон в песочнице
# иначе затрёт настоящие бэкапы копией из песочницы — то есть сломает путь отката
# ровно тем действием, которое затевалось, чтобы ничего не сломать.
BACKUP_ROOT="${SINGULARITY_BACKUP_ROOT:-$TARGET_HOME/.singularity-tasks-backup}"

# имя|признак установленного инструмента|куда класть скилл
# bash 3.2 (штатный на macOS) не умеет ассоциативные массивы — держим строкой
# Признак у agents — сам $TARGET_HOME: общий каталог нужен любому из четырёх
# читающих его инструментов, и на свежей машине ~/.agents может ещё не быть.
TARGETS="agents|$TARGET_HOME|$TARGET_HOME/.agents/skills/singularity-tasks
claude|$TARGET_HOME/.claude|$TARGET_HOME/.claude/skills/singularity-tasks
antigravity|$TARGET_HOME/.gemini/config|$TARGET_HOME/.gemini/config/skills/singularity-tasks"

# Прежние цели: эти инструменты читают ~/.agents/skills, своя копия даёт дубль.
LEGACY="codex|$TARGET_HOME/.codex/skills/singularity-tasks
opencode|$TARGET_HOME/.config/opencode/skills/singularity-tasks
qwen|$TARGET_HOME/.qwen/skills/singularity-tasks"

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

# Раскатка из git worktree затрёт три каталога раскатки (agents, claude, antigravity) содержимым
# отдельной ветки — вместе с незакоммиченной работой того, кто в этом worktree
# сидит, и поверх работы параллельных сессий. Сверять из worktree можно и нужно,
# раскатывать — только осознанно.
if [[ "$mode" == "install" && $force -eq 0 && -f "$SRC/.git" ]]; then
    cat >&2 <<EOF
Отказ: это git worktree ($SRC), а не основной рабочий каталог репозитория.
Раскатка отсюда зальёт в три каталога раскатки (agents, claude, antigravity) содержимое этой ветки,
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
                # убивают скрипт ровно здесь — то есть тогда, когда работа нужнее всего.
                # sed, а не head: head выходит после N строк, diff ловит SIGPIPE
                # (141), и pipefail + set -e роняют скрипт на длинном диффе
                { diff -r -u "${DIFF_EXCL[@]}" "$SRC/$item" "$dst/$item" 2>&1 || true; } \
                    | sed -n '1,20p' | sed 's/^/        /'
            }
            d=1
        fi
    done
    return $d
}

file_hash() {   # sha256 файла; один результат на macOS (shasum) и Linux (sha256sum)
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        sha256sum "$1" | cut -d' ' -f1
    fi
}

hash_of() {   # хеш файла; пусто, если файла нет
    [[ -f "$1" ]] || return 0
    file_hash "$1"
}

# Запись последней раскатки: хеши файлов ЦЕЛИ такими, какими раскатка их оставила.
# Запись пишет сама раскатка — подложенную запись проверка не отличит, но и
# подкладывать её незачем: раскатка от этого не становится опаснее, чем была.
# Файл живёт в каталоге цели и не входит в ITEMS, поэтому сверка его не видит.
write_record() {   # $1 — каталог цели
    local dst="$1" manifest="$1/.singularity-applied" item f rel
    {
        for item in "${ITEMS[@]}"; do
            if [[ -d "$dst/$item" ]]; then
                while IFS= read -r -d '' f; do
                    rel="${f#"$dst"/}"
                    case "$rel" in *__pycache__*|*.pyc|*/.DS_Store|.DS_Store) continue ;; esac
                    printf '%s=%s\n' "$rel" "$(file_hash "$f")"
                done < <(find "$dst/$item" -type f -print0 | sort -z)
            elif [[ -f "$dst/$item" ]]; then
                # ITEMS — и файлы (SKILL.md, README.md), и каталоги
                printf '%s=%s\n' "$item" "$(file_hash "$dst/$item")"
            fi
        done
    } > "$manifest.tmp"
    # атомарная замена: раскатка, оборванная на полупиши, не оставляет
    # обрезанной записи, которая дала бы ложный отказ на следующем запуске
    mv "$manifest.tmp" "$manifest"
}

record_ok() {   # 0 — запись годна, 1 — битая (строки не path=sha256)
    local line h
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        h="${line##*=}"
        [[ "$line" == *=* && "$h" =~ ^[0-9a-f]{64}$ ]] || return 1
    done < "$1"
    return 0
}

record_hash() {   # $1 запись, $2 путь; печатает хеш (пусто, если пути нет в записи)
    local line h
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        h="${line##*=}"
        [[ "${line%="$h"}" == "$2" ]] && { printf '%s' "$h"; return 0; }
    done < "$1"
    printf ''
    return 0
}

# Чужая правка в файле: его содержимое нет ни в записи (последняя раскатка),
# ни в эталоне (рабочее дерево) — раскатка затрёт его. Сравнение ПО ФАЙЛУ, а не
# по цели целиком: цель может расходиться с эталоном из-за СВОЕЙ нераскатанной
# правки, неся чужую правку в другом файле, — цель-целиком такое назовёт
# «чужим», и штатный выход «перенеси в репозиторий и раскатай» сам упрётся
# в отказ.
foreign_file() {   # $1 каталог цели, $2 запись, $3 путь (относительно цели); 0 — чужая
    local th rh sh
    th="$(file_hash "$1/$3")"
    rh="$(record_hash "$2" "$3")"
    sh="$(hash_of "$SRC/$3")"
    [[ "$th" != "$rh" && "$th" != "$sh" ]]
}

# Чужие правки в цели: 0 — нет, 1 — есть (печатает какие файлы),
# 2 — записи нет или она битая (сверить нечем).
target_foreign_files() {   # $1 — каталог цели
    local dst="$1" manifest="$1/.singularity-applied" item f rel line h d=0
    [[ -f "$manifest" ]] || return 2
    record_ok "$manifest" || return 2
    for item in "${ITEMS[@]}"; do
        if [[ -d "$dst/$item" ]]; then
            while IFS= read -r -d '' f; do
                rel="${f#"$dst"/}"
                case "$rel" in *__pycache__*|*.pyc|*/.DS_Store|.DS_Store) continue ;; esac
                if foreign_file "$dst" "$manifest" "$rel"; then
                    echo "    ✗ чужая правка: $rel"
                    d=1
                fi
            done < <(find "$dst/$item" -type f -print0 | sort -z)
        elif [[ -f "$dst/$item" ]]; then
            if foreign_file "$dst" "$manifest" "$item"; then
                echo "    ✗ чужая правка: $item"
                d=1
            fi
        fi
    done
    # Файл удалён в цели, но есть в эталоне — раскатка восстановит его,
    # то есть затрёт удаление
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        h="${line##*=}"
        rel="${line%="$h"}"
        [[ -e "$dst/$rel" ]] && continue
        if [[ -e "$SRC/$rel" ]]; then
            echo "    ✗ удалён в цели, есть в эталоне: $rel"
            d=1
        fi
    done < "$manifest"
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

# --------------------------------------------------------------------- префлайт
# Раскатка перезаписывает каталоги целей целиком. Расхождение цели с эталоном —
# либо своя нераскатанная правка (цель = запись последней раскатки), либо правка
# в цели, которую раскатка затрёт молча (бэкап есть, но его никто не смотрит).
# Запись (.singularity-applied в цели) различает их: сверять нужно именно с ней,
# а не с эталоном — на нераскатанной правке сверка с эталоном всегда красная.
if [[ "$mode" == "install" && $force -eq 0 ]]; then
    foreign=()
    while IFS='|' read -r name probe dst; do
        [[ -z "$name" ]] && continue
        [[ -n "$only" && "$only" != "$name" ]] && continue
        [[ -d "$probe" ]] || continue
        [[ -d "$dst" ]] || continue          # цели нет — чистая установка, терять нечего
        target_drift "$dst" quiet && continue  # = эталону — раскатывать нечего, терять нечего
        r=0; target_foreign_files "$dst" >/dev/null || r=$?
        case $r in
            0) ;;   # каждое отличие — от последней раскатки или уже перенесено в эталон
            2) echo "⚠ $name: записи последней раскатки нет (или она битая) — правки в цели проверить нечем, раскатываю; запись будет (пере)писана."
               ;;
            *) foreign+=("$name") ;;
        esac
    done <<< "$TARGETS"
    if [[ ${#foreign[@]} -gt 0 ]]; then
        for name in "${foreign[@]}"; do
            while IFS='|' read -r n probe dst; do
                [[ "$n" == "$name" ]] && break
            done <<< "$TARGETS"
            echo "· $name — в цели правки, которых нет ни в последней раскатке, ни в эталоне (рабочем дереве):"
            # 1 — штатный результат («найдено расхождение»), без || true set -e
            # убил бы скрипт до сообщения об отказе
            { target_foreign_files "$dst" || true; }
            echo "    их раскатка затрёт (цель против эталона):"
            # sed, а не head: head выходит на 31-й строке, левая часть пайпа
            # ловит SIGPIPE (141), и pipefail + set -e роняют скрипт до «Отказ»
            { target_drift "$dst" || true; } | sed -n '1,30p'
        done
        disp=""
        for name in "${foreign[@]}"; do disp="${disp:+$disp, }$name"; done
        cat >&2 <<EOF

Отказ: в целях ($disp) есть правки, которых нет ни в последней раскатке, ни в эталоне.
Раскатка перезаписывает каталоги целей целиком и затрёт их (бэкап будет, но он не заменяет
взгляд на правку). Перенеси правку в репозиторий и раскатай без --force — тогда запись
обновится; либо осознанно: tools/install.sh --force
EOF
        exit 2
    fi
fi

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
                # Расхождение с эталоном само по себе не говорит, ЧТО именно разошлось:
                # своя нераскатанная правка или правка в цели. Запись последней
                # раскатки различает — по файлу: чужим считается файл, чьё
                # содержимое нет ни в записи, ни в эталоне.
                if [[ -z "$quiet" && -d "$dst" ]]; then
                    r=0; target_foreign_files "$dst" >/dev/null || r=$?
                    case $r in
                        1) echo "    в цели при этом есть файлы, которых нет ни в записи последней раскатки (.singularity-applied), ни в эталоне:"
                           { target_foreign_files "$dst" || true; }
                           ;;
                        2) echo "    (записи последней раскатки нет — расхождение может быть как нераскатанной правкой, так и правкой в цели; запись напишет следующая раскатка)"
                           ;;
                    esac
                fi
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
            # Запись пишем и в случае «уже совпадает»: если правку из цели перенесли
            # в репозиторий, цель = эталону, но старая запись о ней молчит — и без
            # обновления следующая раскатка наслала бы ложный отказ.
            write_record "$dst"
            ;;
    esac
done <<< "$TARGETS"

# Лишние копии на прежних местах. Нет копии — нет и строки: это норма, а не цель.
while IFS='|' read -r name dst; do
    [[ -z "$name" ]] && continue
    [[ -n "$only" && "$only" != "$name" ]] && continue
    any=1
    [[ -e "$dst" || -L "$dst" ]] || continue
    case "$mode" in
        list)
            printf "· %-12s %-8s %s\n" "$name" "ЛИШНЯЯ" "$dst"
            ;;
        check)
            [[ -n "$quiet" ]] || echo "· $name — лишняя копия $dst: инструмент читает ~/.agents/skills, скилл виден дважды"
            drift=1
            drift_names="${drift_names:+$drift_names, }$name"
            ;;
        install)
            echo "· $name — лишняя копия $dst"
            bak="$BACKUP_ROOT/$name"
            mkdir -p "$BACKUP_ROOT"
            rm -rf "$bak"
            cp -R "$dst" "$bak"
            echo "    бэкап: $bak"
            rm -rf "${dst:?}"
            if [[ -e "$dst" || -L "$dst" ]]; then
                echo "    ✗ не удалилась" >&2
                exit 1
            fi
            echo "    ✓ убрана"
            installed=$((installed + 1))
            ;;
    esac
done <<< "$LEGACY"

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
