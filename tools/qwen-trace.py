#!/usr/bin/env python3
"""Достать из стенограммы Qwen Code, как агент пользовался скиллом singularity-tasks.

Наблюдение за живым агентом: что он звал, что получил в ответ, где споткнулся.
Стенограмма — `~/.qwen/projects/<slug>/chats/<session>.jsonl`, десятки мегабайт,
поэтому читаем построчно и фильтруем по подстроке до разбора JSON.

    tools/qwen-trace.py                      # последняя сессия, вызовы sing.py
    tools/qwen-trace.py --grep 'sing.py'     # своя подстрока
    tools/qwen-trace.py --all                # все вызовы инструментов
    tools/qwen-trace.py --since 16:00        # только свежее (UTC, как в стенограмме)

Ищем self-совпадения осознанно: строка с текстом рассуждения агента тоже содержит
'sing.py', и это не вызов. Поэтому в вывод идут только functionCall/functionResponse,
а размышления — отдельным флагом --think.
"""
import argparse
import glob
import json
import os
import sys

QWEN_PROJECTS = os.path.expanduser("~/.qwen/projects")


def latest_session(slug=None):
    pat = os.path.join(QWEN_PROJECTS, slug or "*", "chats", "*.jsonl")
    files = [f for f in glob.glob(pat) if not f.endswith(".ledger.jsonl")]
    if not files:
        sys.exit(f"стенограмм не найдено: {pat}")
    return max(files, key=os.path.getmtime)


def parts_of(rec):
    return ((rec.get("message") or {}).get("parts")) or []


def short(s, n):
    s = s.replace("\r", "")
    return s if len(s) <= n else s[:n] + f" …[+{len(s) - n} симв.]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="путь к .jsonl (по умолчанию — самая свежая)")
    ap.add_argument("--slug", help="каталог проекта в ~/.qwen/projects")
    ap.add_argument("--grep", default="sing.py", help="подстрока-фильтр по сырой строке")
    ap.add_argument("--all", action="store_true", help="все вызовы инструментов")
    ap.add_argument("--think", action="store_true", help="показывать и рассуждения модели")
    ap.add_argument("--since", help="HH:MM (UTC) — отсечь раннее")
    ap.add_argument("--width", type=int, default=1200, help="обрезка вывода")
    args = ap.parse_args()

    path = args.session or latest_session(args.slug)
    needle = None if args.all else args.grep
    print(f"# {path}\n", file=sys.stderr)

    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            if needle and needle not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = (rec.get("timestamp") or "")[11:19]
            if args.since and ts and ts < args.since:
                continue
            for part in parts_of(rec):
                if "functionCall" in part:
                    fc = part["functionCall"]
                    cmd = (fc.get("args") or {}).get("command") or json.dumps(
                        fc.get("args"), ensure_ascii=False)
                    print(f"[{ts}] #{i} → {fc.get('name')}\n    {short(cmd, args.width)}\n")
                elif "functionResponse" in part:
                    fr = part["functionResponse"]
                    out = ((fr.get("response") or {}).get("output")
                           or json.dumps(fr.get("response"), ensure_ascii=False))
                    status = (rec.get("toolCallResult") or {}).get("status", "")
                    print(f"[{ts}] #{i} ← {fr.get('name')} [{status}]\n"
                          f"    {short(str(out), args.width)}\n")
                elif args.think and part.get("text"):
                    print(f"[{ts}] #{i} · думает\n    {short(part['text'], args.width)}\n")


if __name__ == "__main__":
    main()
