"""
步步伴 StepMate — 后端 v2

统一 OpenAI 兼容协议: Ollama(/v1)、DeepSeek、通义、智谱、硅基流动、OpenRouter、
以及任意自定义接口, 用户在 UI 里选预设或自填 {base, model, key}。

接口:
  GET  /                 -> 前端
  GET  /healthz          -> 桌面壳健康检查
  POST /api/decompose    -> 目标拆步骤树 (level 1-5 控制粒度)
  POST /api/chat         -> 每步执行搭档 (非流式, 兼容/测试用)
  POST /api/chat/stream  -> 同上, SSE 流式
"""

import json
import os
import re
import signal
import threading
import time
import uuid
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI(title="步步伴 StepMate")

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------- 孤儿进程守护（桌面 App 场景） ----------
def _parent_guard() -> None:
    expected = int(os.environ.get("STEPBUDDY_PARENT_PID") or 0)
    if expected <= 0:
        return
    logf = os.environ.get("STEPBUDDY_LOG", "/tmp/stepmate-guard.log")

    def note(msg: str) -> None:
        try:
            with open(logf, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def run() -> None:
        while True:
            time.sleep(1.0)
            if os.getppid() != expected:  # 父进程死后被 launchd 收养 -> ppid 变 1
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except Exception:
                    pass
                note("父进程已退出，后端自行关闭")
                time.sleep(3)
                os._exit(0)

    threading.Thread(target=run, daemon=True).start()
    note(f"守护启动，父 pid={expected}")


_parent_guard()


# ---------- Provider 调用（统一 OpenAI 兼容协议） ----------
def _headers(cfg: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if cfg.get("key"):
        h["Authorization"] = f"Bearer {cfg['key']}"
    return h


def _is_local(cfg: dict) -> bool:
    return "127.0.0.1" in cfg.get("base", "") or "localhost" in cfg.get("base", "")


async def _conn_hint(cfg: dict) -> str:
    if _is_local(cfg):
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
                await c.get(cfg["base"].split("/v1")[0].rstrip("/") + "/api/tags")
            return (
                f"本地 AI 服务 ({cfg['base']}) 连不上。\n"
                "若用的是 Ollama：打开 Ollama App（菜单栏出现羊驼图标即可），然后重试。"
            )
        except Exception:
            return (
                "本地 Ollama 没在运行：打开 Ollama App，或在终端运行 `ollama serve`，然后重试。"
            )
    return f"连不上 {cfg['base']}，请检查网络或接口地址。"


def _status_hint(cfg: dict, code: int, body: str) -> str:
    if code == 401 or code == 403:
        return "API Key 无效或没有权限，请检查 Key。"
    if code == 404:
        if _is_local(cfg):
            return f"本地没有这个模型：{cfg['model']}。终端运行 `ollama pull {cfg['model']}` 先下载。"
        return f"接口或模型名不对（404）：{cfg['model']} @ {cfg['base']}"
    if code == 429:
        return "请求太频繁/额度用完（429），稍后再试或换模型。"
    return f"AI 服务返回 {code}：{body[:160]}"


async def _call_provider(cfg: dict, system: str, user: str, history: Optional[list] = None) -> str:
    messages = [{"role": "system", "content": system}]
    if history:
        messages += history
    messages.append({"role": "user", "content": user})
    try:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as c:
            r = await c.post(
                cfg["base"].rstrip("/") + "/chat/completions",
                json={"model": cfg["model"], "messages": messages, "temperature": 0.6},
                headers=_headers(cfg),
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except httpx.ConnectError:
        raise RuntimeError(await _conn_hint(cfg))
    except httpx.HTTPStatusError as e:
        raise RuntimeError(_status_hint(cfg, e.response.status_code, e.response.text))
    except (KeyError, IndexError):
        raise RuntimeError("AI 返回了意外格式，请稍后重试。")


async def _stream_provider(cfg: dict, system: str, user: str, history: Optional[list] = None) -> AsyncIterator[str]:
    messages = [{"role": "system", "content": system}]
    if history:
        messages += history
    messages.append({"role": "user", "content": user})
    url = cfg["base"].rstrip("/") + "/chat/completions"
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0.6, "stream": True}
    try:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as c:
            async with c.stream("POST", url, json=payload, headers=_headers(cfg)) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    yield "data: " + json.dumps({"error": _status_hint(cfg, r.status_code, body)}, ensure_ascii=False) + "\n\n"
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                        if delta:
                            yield "data: " + json.dumps({"t": delta}, ensure_ascii=False) + "\n\n"
                    except Exception:
                        continue
    except httpx.ConnectError:
        yield "data: " + json.dumps({"error": await _conn_hint(cfg)}, ensure_ascii=False) + "\n\n"
        return
    yield "data: [DONE]\n\n"


# ---------- 步骤树解析（容错） ----------
def parse_steps(text: str) -> dict:
    raw = text.strip()
    candidate = raw
    if candidate.startswith("```"):
        parts = candidate.split("```", 2)
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
    try:
        data = json.loads(candidate)
        if isinstance(data, dict) and "steps" in data:
            return _normalize(data)
        if isinstance(data, list):
            return _normalize({"steps": data})
    except Exception:
        pass
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and "steps" in data:
                return _normalize(data)
    except Exception:
        pass
    # JSON 常见笔误修复（模型长输出偶发）: ""key": -> "key":, 尾逗号, 全角引号
    looks_like_json = raw.lstrip().startswith(("{", "["))
    if looks_like_json:
        try:
            # 形态1: 游离引号 + 同行空格 + ""key":   (如 {"  ""title": ...})
            fixed = re.sub(r'"[ \t]*""([A-Za-z_]+)"\s*:', r'"\1":', raw)
            # 形态2: 行首/缩进后的 ""key":          (如 \n    ""title": ...)
            fixed = re.sub(r'""([A-Za-z_]+)"\s*:', r'"\1":', fixed)
            fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
            fixed = fixed.replace("“", '"').replace("”", '"')
            m = re.search(r"\{.*\}", fixed, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                if isinstance(data, dict) and "steps" in data:
                    return _normalize(data)
        except Exception:
            return {"steps": []}  # JSON 碎片绝不当步骤列表
    steps = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*").strip()
        if line:
            steps.append({"title": line[:60], "detail": "", "substeps": []})
    return {"steps": steps[:12]}


def _normalize(data: dict) -> dict:
    steps = []
    for s in data.get("steps", []):
        if not isinstance(s, dict) or not s.get("title"):
            continue
        subs = []
        for ss in s.get("substeps", []) or []:
            if isinstance(ss, dict) and ss.get("title"):
                subs.append({"title": str(ss["title"])[:80], "detail": str(ss.get("detail") or "")[:200]})
        steps.append({"title": str(s["title"])[:80], "detail": str(s.get("detail") or "")[:200], "substeps": subs})
    return {"steps": steps}


LEVELS = {
    1: "3-4 个粗步骤，不带子步骤，只给方向。",
    2: "4-6 个步骤，关键步骤带 1-2 个子步骤。",
    3: "3-8 个主步骤，关键步骤带 1-3 个可操作子步骤。",
    4: "6-9 个步骤，每个步骤都带 2-3 个可立即执行的子步骤。",
    5: "8-12 个步骤，全部带 2-4 个子步骤，子步骤尽量精确到每天可完成的具体行动。",
}


# ---------- 路由 ----------
@app.get("/healthz")
async def healthz():
    """桌面壳用：探测本服务（响应体含 stepmate 以校验身份）。"""
    return {"ok": True, "app": "stepmate"}


@app.post("/api/decompose")
async def decompose(req: Request):
    body = await req.json()
    goal = (body.get("goal") or "").strip()
    if not goal:
        return JSONResponse({"error": "目标不能为空"}, status_code=400)
    cfg = body.get("provider") or {}
    if not cfg.get("base") or not cfg.get("model"):
        return JSONResponse({"error": "请先在设置里选 AI 接口（地址和模型不能为空）"}, status_code=400)
    try:
        level = max(1, min(5, int(body.get("level") or 3)))
    except (TypeError, ValueError):
        level = 3

    system = (
        "你是一个目标拆解教练。用户会给出一个目标或想做成的事。"
        "请把它拆解成清晰、可执行、由小到大的步骤树。"
        f"粒度要求：{LEVELS[level]}\n"
        "只输出 JSON, 结构严格如下, 不要任何解释文字:\n"
        '{"steps":[{"title":"步骤标题","detail":"这一步要做什么(1-2句)",'
        '"substeps":[{"title":"子步骤","detail":""}]}]}\n'
        "要求: 步骤要具体、可马上动手；没有子步骤时 substeps 为空数组。"
    )
    try:
        text = await _call_provider(cfg, system, f"目标: {goal}")
        data = parse_steps(text)
        if not data["steps"]:
            return JSONResponse({"error": "AI 没能拆出有效步骤，换个说法再试一次？"}, status_code=502)
        data["goal"] = goal
        data["level"] = level
        return data
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"AI 调用失败: {e}"}, status_code=500)


def _chat_system(goal: str, step: str, step_detail: str) -> str:
    return (
        "你是用户的『执行搭档』, 不是普通的建议机器人。"
        "你们正在一起完成一个目标, 当前聚焦于下面这个具体步骤。"
        "你的任务: 直接产出用户能拿去用的东西——草稿、清单、话术、代码片段、邮件、检索式、行动计划等,"
        "而不是只说『你应该…』。\n"
        f"总目标: {goal}\n当前步骤: {step}\n步骤说明: {step_detail}\n"
        "原则: 1) 给可复制的成品; 2) 一次推进一小步; 3) 需要时主动追问关键前提;"
        "4) 中文回复; 5) 若需要写代码/文本, 用代码块包裹方便复制。"
    )


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    step = body.get("step", "")
    user_msg = (body.get("message") or "").strip()
    if not step or not user_msg:
        return JSONResponse({"error": "缺少步骤或消息"}, status_code=400)
    cfg = body.get("provider") or {}
    if not cfg.get("base") or not cfg.get("model"):
        return JSONResponse({"error": "请先在设置里选 AI 接口"}, status_code=400)
    system = _chat_system(body.get("goal", ""), step, body.get("step_detail", ""))
    try:
        reply = await _call_provider(cfg, system, user_msg, history=body.get("history") or [])
        return {"reply": reply}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"AI 调用失败: {e}"}, status_code=500)


@app.post("/api/chat/stream")
async def chat_stream(req: Request):
    body = await req.json()
    step = body.get("step", "")
    user_msg = (body.get("message") or "").strip()
    cfg = body.get("provider") or {}
    if not step or not user_msg or not cfg.get("base") or not cfg.get("model"):
        return JSONResponse({"error": "缺少参数或 AI 接口配置"}, status_code=400)
    system = _chat_system(body.get("goal", ""), step, body.get("step_detail", ""))

    async def gen():
        async for chunk in _stream_provider(cfg, system, user_msg, history=body.get("history") or []):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(LOCAL_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
