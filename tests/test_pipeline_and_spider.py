"""End to end: runner + analysis + dashboard, a real provider against a mock HTTP server, and the
Spider port on a small Spider-format fixture."""
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from sql_agent.analysis import analyze
from sql_agent.runner import run


@pytest.fixture
def mini_bench(tmp_path, bench):
    b = {"answerable": bench["answerable"][:6] + [q for q in bench["answerable"] if q["id"] == "m08"],
         "ambiguous": bench["ambiguous"][:1], "unanswerable": bench["unanswerable"][:1],
         "adversarial": [q for q in bench["adversarial"] if q["id"] in ("d01", "d05")]}
    p = tmp_path / "q.yaml"
    p.write_text(yaml.safe_dump(b))
    return p, b


def test_runner_analysis_dashboard_and_resume(db, tmp_path, mini_bench):
    qpath, b = mini_bench
    cfg = {"name": "t", "trials": 1, "concurrency": 2, "questions": str(qpath),
           "agents": [{"provider": "scripted", "policy": "oracle"}, {"provider": "scripted", "policy": "noisy"}],
           "systems": ["schema_prompt", "verified"], "studies": {"guardrails": True}}
    c = tmp_path / "c.yaml"
    c.write_text(yaml.safe_dump(cfg))
    out = run(str(c), str(tmp_path / "out"), quiet=True)
    n = len((out / "traces.jsonl").read_text().splitlines())
    assert n == 2 * 2 * sum(len(v) for v in b.values())
    m = json.loads((out / "manifest.json").read_text())
    assert m["db_unchanged_by_agent_runs"] and m["scripted_only"]
    S = analyze(out)
    for f in ("report.md", "summary.json", "dashboard.html", "guardrails.json", "figures/accuracy.png"):
        assert (out / f).exists(), f
    oracle = [r for r in S["rows"] if r["model"] == "scripted:oracle"]
    assert all(r["ex"]["rate"] == 1 and r["harmful_sql_ran"] == 0 for r in oracle)
    assert json.loads((out / "guardrails.json").read_text())["summary"]["full_stack"] == 34
    run(str(c), str(tmp_path / "out"), quiet=True)  # resume: nothing re-run
    assert len((out / "traces.jsonl").read_text().splitlines()) == n


class _Mock(BaseHTTPRequestHandler):
    gold = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        prompt = body["messages"][-1]["content"]
        q = next((line[10:] for line in prompt.splitlines() if line.startswith("Question: ")), "")
        text = json.dumps({"sql": self.gold.get(q, "SELECT 1")})
        data = json.dumps({"content": [{"type": "text", "text": text}],
                           "usage": {"input_tokens": 500, "output_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_real_provider_code_path_with_mock_server(db, tmp_path, mini_bench, monkeypatch):
    qpath, b = mini_bench
    _Mock.gold = {q["question"]: q["sql"] for q in b["answerable"]}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    cfg = {"name": "m", "questions": str(qpath), "kinds": ["answerable"], "systems": ["verified"],
           "cache_dir": str(tmp_path / "cache"),
           "agents": [{"provider": "anthropic", "model": "mock", "base_url": f"http://127.0.0.1:{srv.server_port}",
                       "name": "mock"}]}
    c = tmp_path / "c.yaml"
    c.write_text(yaml.safe_dump(cfg))
    out = run(str(c), str(tmp_path / "o"), quiet=True)
    srv.shutdown()
    rows = [json.loads(x) for x in (out / "traces.jsonl").read_text().splitlines()]
    assert rows and all(r["correct"] and r["in_tokens"] == 500 for r in rows)
    assert json.loads((out / "manifest.json").read_text())["usage"]["in_tokens"] == 500 * len(rows)


def _spider_fixture(root):
    d = root / "database" / "concerts"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "concerts.sqlite")
    con.executescript("""
      CREATE TABLE singer (singer_id INTEGER PRIMARY KEY, name VARCHAR(40), age INT, country TEXT);
      CREATE TABLE concert (concert_id INTEGER, singer_id INT, year TEXT, attendance REAL);
      INSERT INTO singer VALUES (1,'Alice',31,'France'),(2,'bob',25,'USA'),(3,'Chen',40,'China'),(4,'ana',29,'USA');
      INSERT INTO concert VALUES (1,1,'2014',1200.5),(2,1,'2015',900),(3,3,'2014',3000),(4,2,'2016',NULL);""")
    con.commit()
    con.close()
    items = [
        {"db_id": "concerts", "question": "How many singers are there?", "query": "SELECT count(*) FROM singer"},
        {"db_id": "concerts", "question": "Average age of singers from USA?",
         "query": "SELECT avg(age) FROM singer WHERE country = 'USA'"},
        {"db_id": "concerts", "question": "Names of singers with a concert in 2014, by name.",
         "query": "SELECT T1.name FROM singer AS T1 JOIN concert AS T2 ON T1.singer_id = T2.singer_id "
                  "WHERE T2.year = '2014' ORDER BY T1.name"},
        # SQLite LIKE is case-insensitive, PostgreSQL LIKE is not: this one must be dropped, not silently kept
        {"db_id": "concerts", "question": "How many singers have a name starting with a?",
         "query": "SELECT count(*) FROM singer WHERE name LIKE 'a%'"},
    ]
    (root / "dev.json").write_text(json.dumps(items))


def test_spider_port_keeps_only_questions_that_agree(db, tmp_path):
    from sql_agent.db import PG, DBError
    from sql_agent.spider import ADMIN_DSN, main
    try:
        PG(ADMIN_DSN, read_only=False).value("SELECT 1")
    except DBError:
        pytest.skip("owner role not reachable; set SQLAGENT_ADMIN_DSN")
    _spider_fixture(tmp_path)
    out = tmp_path / "spider.yaml"
    main(["--spider-dir", str(tmp_path), "--out", str(out)])
    y = yaml.safe_load(out.read_text())
    assert len(y["answerable"]) == 3 and y["answerable"][0]["schema"] == "spider_concerts"
    assert len(y["dropped"]) == 1 and "differ" in y["dropped"][0]["reason"]
    cfg = {"name": "sp", "questions": str(out), "kinds": ["answerable"], "systems": ["verified"],
           "agents": [{"provider": "scripted", "policy": "oracle"}]}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(cfg))
    res = run(str(tmp_path / "c.yaml"), str(tmp_path / "o"), quiet=True)
    rows = [json.loads(x) for x in (res / "traces.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and all(r["correct"] for r in rows)
    PG(ADMIN_DSN, read_only=False).raw("DROP SCHEMA spider_concerts CASCADE")
    assert os.path.exists(out)
