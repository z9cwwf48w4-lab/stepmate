"""
步步伴 StepMate — 后端
本地目标分解 + 每步 AI 陪伴执行搭档

双后端:
  - local  : 本机 Ollama (qwen3:8b-16k), 免费, 零密钥
  - deepseek: DeepSeek Chat API (https://api.deepseek.com), 需 API key

接口:
  GET  /                -> 返回前端
  POST /api/decompose   -> 把目标拆成结构化步骤树
  POST /api/chat        -> 针对某一步, AI 陪伴执行 (产出可复制交付物)
"""

import json
import os
import re
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="步步伴 StepMate")

# ---------- 配置 ----------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b-16k")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------- LLM 调用封装 ----------
async def call_ollama(system: str, user: str, expect_json: bool = False) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.6},
    }
    # 注意: Ollama 的 format:"json" 在 qwen3:8b-16k 上会返回空响应,
    # 因此不依赖该参数, 改为提示词约束 + 后端容错解析。
    # trust_env=False: 避免本机代理(env proxy)把 127.0.0.1 请求拐去死代理。
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]


async def call_deepseek(system: str, user: str, api_key: str, history=None) -> str:
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.6,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # trust_env=False: 同样避免本机代理干扰外网请求
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        r = await c.post(DEEPSEEK_URL, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def backend_dispatch(backend: str, api_key: Optional[str]):
    if backend == "deepseek":
        if not api_key:
            raise ValueError("使用 DeepSeek 需要填写 API Key")
        return "deepseek", api_key
    return "local", None


# ---------- 解析步骤树 ----------
def parse_steps(text: str) -> dict:
    """尽量从 LLM 输出里抽出结构化步骤树；容错降级成纯文本列表。"""
    raw = text.strip()
    # 先尝试直接解析
    candidate = raw
    # 去掉可能的 markdown 代码围栏
    if candidate.startswith("```"):
        parts = candidate.split("```", 2)
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
    try:
        data = json.loads(candidate)
        if isinstance(data, dict) and "steps" in data:
            return data
        if isinstance(data, list):
            return {"steps": data}
    except Exception:
        pass
    # 抠出第一个 {...} JSON 块（模型可能前后夹带废话）
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and "steps" in data:
                return data
            if isinstance(data, list):
                return {"steps": data}
    except Exception:
        pass
    # 降级：按行解析 - 标题
    steps = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*").strip()
        if line:
            steps.append({"title": line, "detail": "", "substeps": []})
    return {"steps": steps}


# ---------- 路由 ----------
@app.post("/api/decompose")
async def decompose(req: Request):
    body = await req.json()
    goal = (body.get("goal") or "").strip()
    backend = body.get("backend", "local")
    api_key = body.get("api_key")
    if not goal:
        return JSONResponse({"error": "目标不能为空"}, status_code=400)

    try:
        bk, key = backend_dispatch(backend, api_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    system = (
        "你是一个目标拆解教练。用户会给出一个目标或想做成的事。"
        "请把它拆解成清晰、可执行、由小到大的步骤树。"
        "只输出 JSON, 结构严格如下, 不要任何解释文字:\n"
        "{\"steps\":[{\"title\":\"步骤标题\",\"detail\":\"这一步要做什么(1-2句)\","
        "\"substeps\":[{\"title\":\"子步骤\",\"detail\":\"\"}]}]}\n"
        "要求: 3-8 个主步骤; 关键步骤带 1-3 个可操作子步骤; 步骤要具体、可马上动手。"
    )
    user = f"目标: {goal}"

    try:
        if bk == "deepseek":
            text = await call_deepseek(system, user, key)
        else:
            text = await call_ollama(system, user, expect_json=True)
        data = parse_steps(text)
        data["goal"] = goal
        data["backend"] = bk
        return data
    except Exception as e:
        return JSONResponse({"error": f"AI 调用失败: {e}"}, status_code=500)


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    goal = body.get("goal", "")
    step = body.get("step", "")
    step_detail = body.get("step_detail", "")
    user_msg = (body.get("message") or "").strip()
    history = body.get("history", [])
    backend = body.get("backend", "local")
    api_key = body.get("api_key")

    if not step or not user_msg:
        return JSONResponse({"error": "缺少步骤或消息"}, status_code=400)
    try:
        bk, key = backend_dispatch(backend, api_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    system = (
        "你是用户的『执行搭档』, 不是普通的建议机器人。"
        "你们正在一起完成一个目标, 当前聚焦于下面这个具体步骤。"
        "你的任务: 直接产出用户能拿去用的东西——草稿、清单、话术、代码片段、邮件、检索式、行动计划等,"
        "而不是只说『你应该…』。\n"
        f"总目标: {goal}\n当前步骤: {step}\n步骤说明: {step_detail}\n"
        "原则: 1) 给可复制的成品; 2) 一次推进一小步; 3) 需要时主动追问关键前提;"
        "4) 中文回复; 5) 若需要写代码/文本, 用代码块包裹方便复制。"
    )
    try:
        if bk == "deepseek":
            reply = await call_deepseek(system, user_msg, key, history=history)
        else:
            reply = await call_ollama(system, user_msg)
        return {"reply": reply, "backend": bk}
    except Exception as e:
        return JSONResponse({"error": f"AI 调用失败: {e}"}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(LOCAL_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
