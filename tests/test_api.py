"""StepMate 接口测试（LLM 调用全部 mock，不依赖真实模型）。"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app, parse_steps  # noqa: E402

client = TestClient(app)


# ---------- parse_steps ----------
def test_parse_clean_json():
    out = parse_steps('{"steps":[{"title":"a","detail":"b","substeps":[]}]}')
    assert out["steps"][0]["title"] == "a"


def test_parse_fenced_json():
    out = parse_steps('```json\n{"steps":[{"title":"x"}]}\n```')
    assert out["steps"][0]["title"] == "x"


def test_parse_json_with_prose():
    out = parse_steps('好的，这是结果：\n{"steps":[{"title":"第一步"}]}\n以上。')
    assert out["steps"][0]["title"] == "第一步"


def test_parse_fallback_lines():
    out = parse_steps("- 先做A\n- 再做B")
    assert [s["title"] for s in out["steps"]] == ["先做A", "再做B"]


def test_parse_normalizes_bad_entries():
    out = parse_steps('{"steps":[{"title":"ok","substeps":[{"title":"s1"},{}]},"垃圾",{"detail":"没标题"}]}')
    assert len(out["steps"]) == 1
    assert out["steps"][0]["substeps"][0]["title"] == "s1"


# ---------- healthz ----------
def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["app"] == "stepmate"


# ---------- decompose ----------
PROVIDER = {"base": "http://mock", "model": "m", "key": ""}


def test_decompose_requires_goal():
    r = client.post("/api/decompose", json={"goal": "", "provider": PROVIDER})
    assert r.status_code == 400


def test_decompose_requires_provider():
    r = client.post("/api/decompose", json={"goal": "学游泳", "provider": {}})
    assert r.status_code == 400


def test_decompose_level_clamped(monkeypatch):
    seen = {}

    async def fake_call(cfg, system, user, history=None):
        seen["system"] = system
        return '{"steps":[{"title":"s"}]}'

    monkeypatch.setattr("app._call_provider", fake_call)
    r = client.post("/api/decompose", json={"goal": "g", "level": 99, "provider": PROVIDER})
    assert r.status_code == 200
    assert r.json()["level"] == 5  # 99 -> 钳到 5
    assert "8-12 个步骤" in seen["system"]


def test_decompose_success(monkeypatch):
    async def fake_call(cfg, system, user, history=None):
        return '{"steps":[{"title":"买泳衣","detail":"","substeps":[{"title":"网上比价"}]}]}'

    monkeypatch.setattr("app._call_provider", fake_call)
    r = client.post("/api/decompose", json={"goal": "学游泳", "level": 3, "provider": PROVIDER})
    assert r.status_code == 200
    d = r.json()
    assert d["goal"] == "学游泳"
    assert d["steps"][0]["substeps"][0]["title"] == "网上比价"


def test_decompose_provider_error_becomes_400(monkeypatch):
    async def fake_call(cfg, system, user, history=None):
        raise RuntimeError("本地 Ollama 没在运行")

    monkeypatch.setattr("app._call_provider", fake_call)
    r = client.post("/api/decompose", json={"goal": "g", "provider": PROVIDER})
    assert r.status_code == 400
    assert "Ollama" in r.json()["error"]


# ---------- chat ----------
def test_chat_requires_fields():
    r = client.post("/api/chat", json={"step": "", "message": "", "provider": PROVIDER})
    assert r.status_code == 400


def test_chat_success(monkeypatch):
    async def fake_call(cfg, system, user, history=None):
        assert "执行搭档" in system
        return "好的，这是草稿。"

    monkeypatch.setattr("app._call_provider", fake_call)
    r = client.post("/api/chat", json={"goal": "g", "step": "s", "message": "写一段", "provider": PROVIDER})
    assert r.status_code == 200
    assert r.json()["reply"] == "好的，这是草稿。"


# ---------- chat/stream ----------
def test_stream_emits_sse(monkeypatch):
    async def fake_stream(cfg, system, user, history=None):
        yield 'data: {"t":"你"}\n\n'
        yield 'data: {"t":"好"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("app._stream_provider", fake_stream)
    with client.stream("POST", "/api/chat/stream",
                       json={"goal": "g", "step": "s", "message": "m", "provider": PROVIDER}) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode()
    assert '"t": "你"' in body or '"t":"你"' in body
    assert "[DONE]" in body


def test_parse_repairs_double_quote_typo():
    # 真实模型输出形态: { 换行缩进后跟 ""title":
    broken = '{"steps":[{"title":"a","substeps":[{"title":"x"},{\n    ""title": "b"}]}]}'
    out = parse_steps(broken)
    assert out["steps"][0]["substeps"][1]["title"] == "b"


def test_parse_json_fragment_never_becomes_step_list():
    broken = '{"steps": [ {"title": "a"},'
    out = parse_steps(broken)
    assert out["steps"] == []
