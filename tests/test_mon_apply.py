#!/usr/bin/env python3
"""tools/mon-apply.sh — guarded-хелпер apply-шага (report + set --start).

Фейковый sing.py (переменная SING указывает на него) логирует каждый
вызов (JSON-строка в FAKE_SING_LOG) и эмулирует доску состоянием
FAKE_SING_STATE: report дописывает в note (кроме swallow_report=1),
set пишет start (кроме ignore_set=1), show возвращает текущую карту.
MON_APPLY_SLEEP=0 обнуляет все паузы хелпера.

Регрессия инцидента 10-30 (батч web-shell 1): цикл искал файлы
${id:0:11}.txt, а файлы созданы как ${id:0:10}.txt — cat в $(cat …)
падал молча, батч отправил ПУСТЫЕ отчёты на все 13 карт и записал
«ok» по rc=0. Здесь: preflight отказывает ДО любого API-вызова, а
верификация по маркеру ловит тихий пустой отчёт (NO-REPORT, красный
код выхода) — оба дефекта инцидента закрыты механизмом, не вниманием.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HELPER = os.path.join(REPO, "tools", "mon-apply.sh")

CARD_A = "T-489e12df-9c83-4930-aee7-33b84d55a911"
CARD_B = "T-4c578c80-bacd-472d-8c23-6df6049c7042"
START = "2026-11-07"
MARKER = "чек 11-06"

FAKE_SING = """\
import json, os, sys

state_path = os.environ["FAKE_SING_STATE"]
log_path = os.environ["FAKE_SING_LOG"]
with open(state_path, encoding="utf-8") as fh:
    state = json.load(fh)
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")

def save():
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)

args = sys.argv[1:]
cmd = args[0]
def task(cid):
    return state["tasks"].setdefault(cid, {"note": [], "start": None})
if cmd == "show":
    cid = args[1]
    t = task(cid)
    print(json.dumps({"id": cid, "note": t["note"], "start": t["start"]},
                     ensure_ascii=False))
elif cmd == "report":
    cid, text = args[1], args[2]
    if state.get("report429", 0) > 0:
        state["report429"] -= 1
        save()
        print("HTTP 429 ThrottlerException (fake)")
        sys.exit(1)
    t = task(cid)
    if not state.get("swallow_report"):
        t["note"].append({"insert": text + "\\n"})
    save()
    print(cid + " — отчёт дописан в заметку")
elif cmd == "set":
    cid = args[1]
    i = args.index("--start")
    if state.get("set429", 0) > 0:
        state["set429"] -= 1
        save()
        print("HTTP 429 ThrottlerException (fake)")
        sys.exit(1)
    t = task(cid)
    if not state.get("ignore_set"):
        t["start"] = args[i + 1]
    save()
    print(cid + " — start=" + args[i + 1])
else:
    print("fake sing.py: неизвестная команда " + cmd)
    sys.exit(2)
"""


class MonApplyTest(unittest.TestCase):
    """Хелпер не должен ни на минуту оставаться слепым к пустому отчёту."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sing-monapply-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reports = os.path.join(self.tmp, "reports")
        os.makedirs(self.reports)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.log_path = os.path.join(self.tmp, "calls.log")
        self.fake = os.path.join(self.tmp, "sing-fake.py")
        with open(self.fake, "w", encoding="utf-8") as fh:
            fh.write(FAKE_SING)
        self.write_state({})

    def write_state(self, extra):
        state = {"tasks": {}}
        state.update(extra)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)

    def read_state(self):
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def read_calls(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def write_reports(self, cards=(CARD_A, CARD_B), missing=(), empty=()):
        for cid in cards:
            path = os.path.join(self.reports, cid[:10] + ".txt")
            if cid in missing:
                continue
            with open(path, "w", encoding="utf-8") as fh:
                if cid in empty:
                    fh.write("")
                else:
                    fh.write(MARKER + ": NO-FIX. issue open, факт один.\n"
                              "- факт два\n- факт три\n")

    def run_helper(self, *args, extra_state=None,
                   cards=(CARD_A, CARD_B)):
        if extra_state is not None:
            self.write_state(extra_state)
        env = dict(os.environ)
        env.update({
            "SING": self.fake,
            "FAKE_SING_STATE": self.state_path,
            "FAKE_SING_LOG": self.log_path,
            "MON_APPLY_SLEEP": "0",
            "SINGULARITY_AGENT": "test-mon-apply",
        })
        cmd = ["bash", HELPER,
               "--reports-dir", self.reports,
               "--start", START, "--marker", MARKER] \
            + list(args) + list(cards)
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_usage_bad_start_format(self):
        self.write_reports()
        p = self.run_helper("--start", "07.11.2026")
        self.assertEqual(p.returncode, 2)
        self.assertIn("формат", p.stderr)

    def test_dry_run_zero_mutations(self):
        self.write_reports()
        p = self.run_helper("--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PLAN-OK", p.stdout)
        self.assertIn("0 API-мутаций", p.stdout)
        calls = self.read_calls()
        self.assertEqual(len(calls), 2, "в dry-run только read-only show")
        for call in calls:
            self.assertEqual(call[0], "show", call)

    def test_preflight_missing_file_no_api(self):
        self.write_reports(missing=(CARD_B,))
        p = self.run_helper()
        self.assertEqual(p.returncode, 1)
        self.assertIn("PREFLIGHT-FAIL", p.stdout)
        self.assertIn("ABORT-PREFLIGHT", p.stdout)
        self.assertEqual(self.read_calls(), [], "refusal before any API call")

    def test_preflight_empty_file_no_api(self):
        self.write_reports(empty=(CARD_A,))
        p = self.run_helper()
        self.assertEqual(p.returncode, 1)
        self.assertIn("PREFLIGHT-FAIL", p.stdout)
        self.assertEqual(self.read_calls(), [], "refusal before any API call")

    def test_full_run_green(self):
        self.write_reports()
        p = self.run_helper()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("TOTAL-BAD: 0", p.stdout)
        self.assertIn("VERDICT: GREEN", p.stdout)
        calls = self.read_calls()
        self.assertEqual([c[0] for c in calls],
                         ["report", "set", "report", "set", "show", "show"])
        self.assertEqual(calls[0][1], CARD_A)
        self.assertEqual(calls[1], ["set", CARD_A, "--start", START])
        self.assertEqual(calls[4][1], CARD_A, "verify идёт по всем картам")
        state = self.read_state()
        for cid in (CARD_A, CARD_B):
            self.assertEqual(state["tasks"][cid]["start"], START)
            note = "".join(o["insert"] for o in state["tasks"][cid]["note"])
            self.assertEqual(note.count(MARKER), 1)

    def test_429_retry(self):
        self.write_reports()
        p = self.run_helper(extra_state={"report429": 1})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        calls = self.read_calls()
        reports_a = [c for c in calls if c[:2] == ["report", CARD_A]]
        self.assertEqual(len(reports_a), 2, "429 → повтор мутации")
        self.assertIn("429", p.stdout)
        self.assertIn("TOTAL-BAD: 0", p.stdout)

    def test_verify_catches_silent_empty_report(self):
        # Инцидент 10-30: report «ok» по rc=0, а в заметку ушло пусто.
        # Теперь верификация по маркеру красит прогон.
        self.write_reports()
        p = self.run_helper(extra_state={"swallow_report": 1})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("NO-REPORT", p.stdout)
        self.assertIn("TOTAL-BAD: 2", p.stdout)
        self.assertIn("VERDICT: RED", p.stdout)

    def test_bad_start(self):
        self.write_reports()
        p = self.run_helper(extra_state={"ignore_set": 1})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("BAD-START", p.stdout)
        self.assertIn("TOTAL-BAD: 2", p.stdout)

    def test_duplicate_marker_on_rerun(self):
        self.write_reports()
        p1 = self.run_helper()
        self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
        p2 = self.run_helper()
        self.assertEqual(p2.returncode, 1, p2.stdout + p2.stderr)
        self.assertIn("DUPLICATE?", p2.stdout)
        self.assertIn("TOTAL-BAD: 2", p2.stdout)


if __name__ == "__main__":
    unittest.main()
