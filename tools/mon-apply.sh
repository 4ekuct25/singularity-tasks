#!/usr/bin/env bash
# mon-apply.sh — apply-шаг мониторинг-sweep-батча: sing.py report + set --start
# по списку карт, с пост-верификацией (инцидент 10-30, батч web-shell 1).
#
#   tools/mon-apply.sh --reports-dir DIR --start YYYY-MM-DD --marker TEXT \
#       [--cards-file FILE] [--dry-run] [T-... ...]
#
# Скоуп — только apply-шаг (report + set --start). GitHub-чеки остаются в
# скриптах батча. Файлы отчётов: DIR/<первые 10 символов id>.txt
# (T-xxxxxxxx.txt), по одному на карту.
#
# Guard'ы (урок инцидента 10-30: цикл искал ${id:0:11}.txt, файлы названы
# ${id:0:10}.txt, cat в $(cat …) падал молча, лог фиксировал «ok» по rc=0):
#   (a) имя отчёт-файла строится из ЕДИНОЙ переменной pre="${id:0:PRE_LEN}" —
#       один срез id на карту, один и тот же файл в preflight, чтении и логах;
#   (b) preflight: test -s на КАЖДЫЙ файл ДО любого API-вызова; пустой/нет —
#       отказ со списком всех виновников (0 API-вызовов);
#   (c) текст отчёта читается в переменную с проверкой rc ($(<файл>)) и
#       повторной проверкой непустоты — никакого слепого $(cat …);
#   (d) интервал ≥ 11 с между API-мутациями; 429 в выводе → 65 с, иначе 30 с;
#       до 3 попыток на мутацию; 3 подряд битых карты → abort всего прогона;
#   (e) пост-верификация по ВСЕМ картам: show --json, маркер отчёта ×1 и
#       start = заданная дата; итог-сводка (ok/bad-списки, TOTAL-BAD).
#
# --dry-run: план всех мутаций + локальный preflight + read-only show (GET)
# на каждую карту (карта резолвится, виден текущий start). 0 API-мутаций.
#
# Коды: 0 — зелёный (всё применено и проверено; в dry-run — план валиден),
#       1 — preflight / мутации / верификация красные, 2 — ошибка вызова.
#
# Окружение: SINGULARITY_AGENT — как для любых команд sing.py;
#            SING — путь к sing.py (по умолчанию ../scripts/sing.py);
#            MON_APPLY_SLEEP — все паузы в секундах (0 в тестах).
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SING="${SING:-$SCRIPT_DIR/../scripts/sing.py}"

PRE_LEN=10          # префикс имени отчёт-файла: первые 10 символов id
MAX_ATTEMPTS=3      # попыток на одну мутацию
ABORT_STREAK=3      # подряд битых карт до остановки всего прогона
SLEEP_MUTATION=11   # с между API-мутациями (контракт: не меньше 11)
SLEEP_SHOW=11       # с между read-only show (тот же лимит трекера)
SLEEP_429=65        # с на 429 (минутное окно троттлинга)
SLEEP_RETRY=30      # с на прочие ошибки
if [ -n "${MON_APPLY_SLEEP:-}" ]; then
  SLEEP_MUTATION=$MON_APPLY_SLEEP
  SLEEP_SHOW=$MON_APPLY_SLEEP
  SLEEP_429=$MON_APPLY_SLEEP
  SLEEP_RETRY=$MON_APPLY_SLEEP
fi

usage() {
  cat <<'EOF'
Пользование:
  tools/mon-apply.sh --reports-dir DIR --start YYYY-MM-DD --marker TEXT
                     [--cards-file FILE] [--dry-run] [T-... ...]

Apply-шаг мониторинг-sweep-батча: sing.py report + set --start по списку
карт, с пост-верификацией (show --json: маркер ×1 + start).

Аргументы:
  --reports-dir DIR   каталог отчёт-файлов DIR/<первые 10 символов id>.txt
  --start DATE        дата старта для set --start (YYYY-MM-DD)
  --marker TEXT       маркерная строка отчёта для верификации (напр. «чек 11-06»)
  --cards-file FILE   файл с id карт (по одному на строку, # — комментарии)
  --dry-run           план + preflight + read-only show (GET); 0 мутаций
  T-...               id карт (можно миксовать с --cards-file)

Коды: 0 — зелёный, 1 — preflight/мутации/верификация красные, 2 — вызов.
EOF
}

die() { # текст код
  printf 'mon-apply: %s\n' "$1" >&2
  exit "$2"
}

REPORTS_DIR=""
START=""
MARKER=""
CARDS_FILE=""
DRY_RUN=0
CARDS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --reports-dir)  [ $# -ge 2 ] || die "нужен аргумент: $1" 2
                    REPORTS_DIR=$2; shift 2 ;;
    --start)        [ $# -ge 2 ] || die "нужен аргумент: $1" 2
                    START=$2; shift 2 ;;
    --marker)       [ $# -ge 2 ] || die "нужен аргумент: $1" 2
                    MARKER=$2; shift 2 ;;
    --cards-file)   [ $# -ge 2 ] || die "нужен аргумент: $1" 2
                    CARDS_FILE=$2; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    -*)             usage >&2; die "неизвестный аргумент: $1" 2 ;;
    *)              CARDS+=("$1"); shift ;;
  esac
done

[ -n "$REPORTS_DIR" ] || { usage >&2; die "нет --reports-dir" 2; }
[ -d "$REPORTS_DIR" ] || die "каталог отчётов не найден: $REPORTS_DIR" 2
[[ $START =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--start: формат YYYY-MM-DD (получено: $START)" 2
[ -n "$MARKER" ] || die "нет --marker" 2
case "$MARKER" in
  *$'\n'*) die "--marker: одна строка" 2 ;;
esac
if [ -n "$CARDS_FILE" ]; then
  [ -f "$CARDS_FILE" ] || die "файл карт не найден: $CARDS_FILE" 2
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%%#*}
    line=$(printf '%s' "$line" | tr -d '[:space:]')
    [ -n "$line" ] && CARDS+=("$line")
  done < "$CARDS_FILE"
fi
[ ${#CARDS[@]} -gt 0 ] || { usage >&2; die "нет карт (позиционные или --cards-file)" 2; }
[ -f "$SING" ] || die "sing.py не найден: $SING (переменная SING)" 2

CARD_RE='^T-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
declare -A SEEN=()
for id in "${CARDS[@]}"; do
  [[ $id =~ $CARD_RE ]] || die "id карты в неверном формате T-<uuid>: $id" 2
  [ -z "${SEEN[$id]:-}" ] || die "карта в списке дважды: $id" 2
  SEEN[$id]=1
done

# Единый префикс имени файла: один срез id на карту, одна переменная —
# тот же префикс в preflight, чтении, логах и верификации (guard (a)).
pre_of() { printf '%s' "${1:0:PRE_LEN}"; }

# API-вызов с retry (guard (d)): до MAX_ATTEMPTS; 429 в выводе → SLEEP_429,
# иначе SLEEP_RETRY. Вывод последнего вызова — в REPLY_OUT.
REPLY_OUT=""
api_call() { # ИМЯ КОМАНДА...
  local name=$1 a rc
  shift
  REPLY_OUT=""
  for ((a = 1; a <= MAX_ATTEMPTS; a++)); do
    if REPLY_OUT=$("$@" 2>&1); then
      printf '  %s ok (attempt %d)\n' "$name" "$a"
      return 0
    fi
    rc=$?
    printf '  %s attempt=%d rc=%d\n' "$name" "$a" "$rc"
    printf '%s\n' "$REPLY_OUT" | sed 's/^/    /'
    if ((a < MAX_ATTEMPTS)); then
      case "$REPLY_OUT" in
        *429*) printf '  429 — пауза %d с\n' "$SLEEP_429"; sleep "$SLEEP_429" ;;
        *)     printf '  пауза %d с\n' "$SLEEP_RETRY"; sleep "$SLEEP_RETRY" ;;
      esac
    fi
  done
  printf '  %s FAIL\n' "$name"
  return 1
}

# Guard (b): preflight ДО любого API-вызова; список всех виновников.
prefail=0
for id in "${CARDS[@]}"; do
  f="$REPORTS_DIR/$(pre_of "$id").txt"
  if [ ! -s "$f" ]; then
    printf 'PREFLIGHT-FAIL: %s — отсутствует или пуст\n' "$f"
    prefail=1
  fi
done
if [ "$prefail" -eq 1 ]; then
  printf 'ABORT-PREFLIGHT: файлов исправь и перегони (0 API-вызовов сделано)\n'
  exit 1
fi
printf 'preflight ok: %d файлов отчётов непусты\n' "${#CARDS[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
  printf '\nDRY-RUN: план мутаций (0 API-мутаций)\n'
  printf 'карт: %d, reports-dir: %s, start: %s, маркер: «%s»\n' \
    "${#CARDS[@]}" "$REPORTS_DIR" "$START" "$MARKER"
  n=0
  dryfail=0
  for id in "${CARDS[@]}"; do
    pre=$(pre_of "$id")
    f="$REPORTS_DIR/$pre.txt"
    n=$((n + 1))
    printf '\n[%d/%d] %s\n' "$n" "${#CARDS[@]}" "$id"
    printf '  файл:   %s (%s байт)\n' "$f" "$(wc -c < "$f")"
    printf '  report: sing.py report %s ← файл (первая строка: %s)\n' \
      "$id" "$(head -n 1 "$f")"
    printf '  set:    sing.py set %s --start %s\n' "$id" "$START"
    printf '  verify: «%s» ×1 + start=%s\n' "$MARKER" "$START"
    # Read-only сверка: карта резолвится (GET, мутаций нет).
    if api_call "show $pre (read-only)" python3 "$SING" show "$id" --json; then
      cur=$(printf '%s\n' "$REPLY_OUT" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("start"))
except Exception:
    print("JSON-ERROR")
')
      printf '  карта резолвится, текущий start=%s\n' "$cur"
    else
      printf '  карта НЕ резолвится (show) — план красный\n'
      dryfail=1
    fi
    sleep "$SLEEP_SHOW"
  done
  if [ "$dryfail" -eq 1 ]; then
    printf '\nDRY-RUN-RED: не все карты резолвятся\n'
    exit 1
  fi
  printf '\nPLAN-OK: %d карт, preflight ok, 0 API-мутаций\n' "${#CARDS[@]}"
  exit 0
fi

# --- живой прогон ---
VDIR="$REPORTS_DIR/verify-$$"
mkdir -p "$VDIR"
OK_CARDS=()
FAIL_CARDS=()
streak=0
n=0
for id in "${CARDS[@]}"; do
  pre=$(pre_of "$id")
  f="$REPORTS_DIR/$pre.txt"
  n=$((n + 1))
  printf '\n[%d/%d] %s\n' "$n" "${#CARDS[@]}" "$id"
  # Guard (c): чтение файла в переменную с проверкой rc и непустоты.
  if ! text=$(<"$f") || [ -z "$text" ]; then
    printf '  READ-FAIL: файл не прочитан или пуст после preflight\n'
    FAIL_CARDS+=("$pre (read)")
    streak=$((streak + 1))
    [ "$streak" -ge "$ABORT_STREAK" ] && { printf 'ABORT: %d подряд битых карт — стоп\n' "$streak"; break; }
    continue
  fi
  card_fail=""
  if ! api_call "report $pre" python3 "$SING" report "$id" "$text"; then
    card_fail="report"
  else
    sleep "$SLEEP_MUTATION"
    if ! api_call "set $pre" python3 "$SING" set "$id" --start "$START"; then
      card_fail="set"
    fi
    sleep "$SLEEP_MUTATION"
  fi
  if [ -n "$card_fail" ]; then
    printf '  карта %s: %s не прошла — карта НЕ применена (set --start за ней не гонялся)\n' \
      "$pre" "$card_fail"
    FAIL_CARDS+=("$pre ($card_fail)")
    streak=$((streak + 1))
    if [ "$streak" -ge "$ABORT_STREAK" ]; then
      printf 'ABORT: %d подряд битых карт — стоп, API не отвечает\n' "$streak"
      break
    fi
    continue
  fi
  OK_CARDS+=("$pre")
  streak=0
done

# Guard (e): пост-верификация по ВСЕМ картам (show — GET, тот же ритм).
printf '\n--- verify: show --json по %d картам -> %s\n' "${#CARDS[@]}" "$VDIR"
for id in "${CARDS[@]}"; do
  pre=$(pre_of "$id")
  if api_call "show $pre (verify)" python3 "$SING" show "$id" --json; then
    printf '%s\n' "$REPLY_OUT" > "$VDIR/$pre.json"
  else
    printf '  show %s FAIL — файла нет, карта уйдёт в NO-SHOW\n' "$pre"
  fi
  sleep "$SLEEP_SHOW"
done

VERIFY_RC=0
python3 - "$VDIR" "$MARKER" "$START" "${CARDS[@]}" <<'PY' || VERIFY_RC=$?
import json
import os
import sys

vdir, marker, start = sys.argv[1], sys.argv[2], sys.argv[3]
cards = sys.argv[4:]
bad = 0
for cid in cards:
    pre = cid[:10]
    path = os.path.join(vdir, pre + ".json")
    if not os.path.exists(path):
        print(f"{pre}: NO-SHOW (файла show нет)")
        bad += 1
        continue
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:  # noqa: BLE001 — повреждённый JSON и есть находка
        print(f"{pre}: JSON-ERROR {e}")
        bad += 1
        continue
    note = d.get("note") or ""
    if isinstance(note, list):
        txt = "".join(o.get("insert", "") if isinstance(o, dict) else str(o)
                      for o in note)
    else:
        txt = str(note)
    c = txt.count(marker)
    status = "OK"
    if c == 0:
        status = "NO-REPORT"
        bad += 1
    elif c > 1:
        status = f"DUPLICATE? ×{c}"
        bad += 1
    got = d.get("start")
    if got != start:
        status += f" BAD-START (start={got})"
        bad += 1
    print(f"{pre}: «{marker}»×{c} start={got} {status}")
print(f"TOTAL-BAD: {bad}")
sys.exit(1 if bad else 0)
PY

printf '\n==== mon-apply summary ====\n'
printf 'карт: %d\n' "${#CARDS[@]}"
printf 'применено ok: %d\n' "${#OK_CARDS[@]}"
if [ ${#FAIL_CARDS[@]} -gt 0 ]; then
  printf 'FAIL: %d\n' "${#FAIL_CARDS[@]}"
  printf '  - %s\n' "${FAIL_CARDS[@]}"
fi
if [ ${#FAIL_CARDS[@]} -eq 0 ] && [ "$VERIFY_RC" -eq 0 ]; then
  printf 'VERDICT: GREEN (0 FAIL, TOTAL-BAD = 0)\n'
  exit 0
fi
printf 'VERDICT: RED (FAIL = %d, verify rc = %d)\n' "${#FAIL_CARDS[@]}" "$VERIFY_RC"
exit 1
