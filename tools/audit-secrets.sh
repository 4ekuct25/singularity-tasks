#!/usr/bin/env bash
# Аудит секретов перед тем, как что-то уедет из репозитория.
#
#   tools/audit-secrets.sh                    — staged (перед commit)
#   tools/audit-secrets.sh --range A..B       — произвольный диапазон
#   tools/audit-secrets.sh --push [remote]    — перед push: <upstream>...HEAD
#   tools/audit-secrets.sh --all              — вся история
#
# Коды: 0 — чисто, 1 — есть находки, 2 — проверять нечего или ошибка вызова.
#
# ⚠ Перед push проверять ДИАПАЗОН, а не staged. `git diff --cached` покрывает только
# незакоммиченное: если работа уже в коммитах, гейт на staged проходит «чисто», и секрет
# уезжает на remote. Поэтому пустой ввод — это код 2, а НЕ «всё хорошо»: молчаливое
# «ок» на пустом диффе и есть главный способ пропустить секрет.
#
# Идентификаторы сущностей SingularityApp (P-/KS-/T-/A-/Q-/KTS-/N- + uuid) исключены:
# это ссылки на объекты, без токена они ничего не открывают, и `singularity.json`
# коммитится намеренно. Общий шаблон на UUID иначе краснеет на каждом коммите, а гейт,
# который всегда красный, перестают читать.
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mode="staged"; range=""; remote="origin"
case "${1:-}" in
    "")        ;;
    --range)   mode="range"; range="${2:-}"; [[ -z "$range" ]] && { echo "нужен диапазон" >&2; exit 2; } ;;
    --push)    mode="push"; remote="${2:-origin}" ;;
    --all)     mode="all" ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)         echo "неизвестный аргумент: $1" >&2; exit 2 ;;
esac

describe=""
case "$mode" in
    staged) diff=$(git diff --cached); describe="staged-изменения" ;;
    range)  diff=$(git diff "$range"); describe="диапазон $range" ;;
    all)    diff=$(git log -p --all); describe="вся история" ;;
    push)
        branch=$(git rev-parse --abbrev-ref HEAD)
        if git rev-parse --verify --quiet "$remote/$branch" >/dev/null; then
            diff=$(git diff "$remote/$branch...HEAD"); describe="$remote/$branch...HEAD"
        else
            diff=$(git log -p); describe="вся история (upstream $remote/$branch не найден)"
        fi
        ;;
esac

if [[ -z "${diff//[[:space:]]/}" ]]; then
    echo "НЕЧЕГО ПРОВЕРЯТЬ: $describe пуст." >&2
    echo "Это НЕ «чисто». Возможно, работа уже в коммитах — тогда нужен --push или --range." >&2
    exit 2
fi

added=$(printf '%s\n' "$diff" | grep -E '^\+' || true)

hits=0
report() { hits=1; echo "✗ $1"; printf '%s\n' "$2" | sed 's/^/    /' | head -10; }

is_placeholder() {   # плейсхолдеры не считаем находкой
    printf '%s' "$1" | grep -qiE '<[^>]*>|example|change[-_]?me|redacted|placeholder|ВСТАВИТЬ|ТОКЕН|your[-_]|xxx+'
}

check() {            # имя | regex
    local name="$1" re="$2" found
    found=$(printf '%s\n' "$added" | grep -inE "$re" || true)
    [[ -z "$found" ]] && return
    local real=""
    while IFS= read -r line; do
        is_placeholder "$line" || real+="$line"$'\n'
    done <<< "$found"
    [[ -n "${real//[[:space:]]/}" ]] && report "$name" "$real"
}

check "приватный ключ"       'BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY'
check "токен GitHub"         'gh[pousr]_[A-Za-z0-9]{20,}'
check "ключ AWS"             'AKIA[0-9A-Z]{16}'
check "JWT"                  'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'
check "Bearer с значением"   'Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}'
check "присвоение секрета"   '(password|passwd|secret|api[-_]?key|apikey|token|psk|access[-_]key)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9+/=_.-]{12,}'
check "строка подключения"   '[a-z][a-z0-9+.-]*://[^[:space:]/@]+:[^[:space:]/@]+@'

# UUID: только те, что не являются идентификаторами сущностей трекера
uuids=$(printf '%s\n' "$added" \
    | grep -oE '[A-Za-z]{0,4}-?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
    | grep -vE '^(P|KS|T|A|Q|KTS|N)-' | sort -u || true)
if [[ -n "${uuids//[[:space:]]/}" ]]; then
    report "UUID вне идентификаторов сущностей" "$uuids"
fi

# по имени файла ищем носители секретов, а не подстроку «secret» в пути:
# иначе гейт краснеет на себе самом (tools/audit-secrets.sh) и на документации
files=$(git ls-files | grep -iE '(^|/)(secrets?|credentials?)\.(json|ya?ml|env|txt|conf|ini)$|\.pem$|\.key$|\.p12$|\.pfx$|(^|/)\.env(\.|$)|(^|/)id_(rsa|ed25519|ecdsa)$|\.sql$' || true)
if [[ -n "${files//[[:space:]]/}" ]]; then
    report "секрет-файлы под версионным контролем" "$files"
fi

echo
if [[ $hits -eq 1 ]]; then
    echo "НАЙДЕНЫ возможные секреты в: $describe"
    echo "Разобраться до отправки: убрать значение или добавить файл в .gitignore."
    echo "«Это тестовые данные» — не основание отправлять."
    exit 1
fi
echo "✓ чисто: $describe"
