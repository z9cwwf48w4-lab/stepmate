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


def _clean(text: str) -> str:
    """去掉 Qwen3 / DeepSeek 等模型的 <think>…</think> 推理块，避免污染 JSON 解析。"""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<think\s*>?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</think\s*>?", "", text, flags=re.IGNORECASE)
    return text.strip()


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
            return _clean(r.json()["choices"][0]["message"]["content"])
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
                            delta = _clean(delta)
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


PRESETS_LOCAL = os.path.join(LOCAL_DIR, "presets.local.json")


@app.get("/api/presets")
async def presets_local():
    """本地接口预设覆盖（含个人 API Key）。该文件不进 git，密钥只留在本机。"""
    if os.path.exists(PRESETS_LOCAL):
        try:
            with open(PRESETS_LOCAL, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


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


def _plan_brief(plan_context) -> str:
    """把全局计划进度压成一段话, 让 AI 每次开口都知道用户干到哪了。"""
    if not plan_context or not isinstance(plan_context, list):
        return ""
    done = [p.get("title", "") for p in plan_context if p.get("done")]
    todo = [p.get("title", "") for p in plan_context if not p.get("done")]
    parts = [f"共 {len(plan_context)} 步, 已完成 {len(done)} 步。"]
    if done:
        parts.append("已完成: " + "、".join(done) + "。")
    if todo:
        parts.append("待做: " + "、".join(todo) + "。")
    return "".join(parts)


ACTION_PROMPT = (
    "工具能力: 若用户表达了想让你替他执行操作的意图(如『做完了』『帮我加一步』), "
    "你可以在回复最末尾另起一行输出动作指令, 每行一条, 最多 2 条, 格式严格如下:\n"
    "[ACTION] mark_done | 步骤标题\n"
    "[ACTION] add_step | 步骤标题 | 一句说明\n"
    "注意: 标题必须与全局进度里出现的步骤名一致; 界面会先弹确认按钮, 用户确认后才执行; "
    "不要对无关话题滥用动作; 除以上两种外没有其他动作。"
)


def _chat_system(goal: str, step: str, step_detail: str, plan_context=None) -> str:
    return (
        "你是用户的『执行搭档』, 不是普通的建议机器人。"
        "你们正在一起完成一个目标, 当前聚焦于下面这个具体步骤。"
        "你的任务: 直接产出用户能拿去用的东西——草稿、清单、话术、代码片段、邮件、检索式、行动计划等,"
        "而不是只说『你应该…』。\n"
        f"总目标: {goal}\n当前步骤: {step}\n步骤说明: {step_detail}\n"
        f"全局进度(供你参考, 不要重复念叨): {_plan_brief(plan_context) or '未知'}\n"
        "原则: 1) 给可复制的成品; 2) 一次推进一小步; 3) 需要时主动追问关键前提;"
        "4) 中文回复; 5) 若需要写代码/文本, 用代码块包裹方便复制。\n"
        + ACTION_PROMPT
    )


def _plan_system(goal: str, step: str, step_detail: str, plan_context=None) -> str:
    return (
        "你是用户的『规划搭档』, 不是执行者。你们正在讨论一个目标下的某一步骤,"
        "用户想质疑它是否合理、补充细节、敲定具体数字(预算/期限/数量)或调整做法。\n"
        f"总目标: {goal}\n当前步骤: {step}\n步骤说明: {step_detail}\n"
        f"全局进度(供你参考, 不要重复念叨): {_plan_brief(plan_context) or '未知'}\n"
        "你的任务: 像靠谱的搭档一样和用户对话——指出这步可能的问题、给出建议的具体数值区间、帮用户想清楚。\n"
        "原则: 1) 中文; 2) 先确认用户真正想要什么, 再给建议; "
        "3) 涉及数字时给具体可执行的区间而非空话; 4) 用代码块包裹任何清单/公式; 5) 一次推进一小步。\n"
        + ACTION_PROMPT
    )


def _refine_system(step_title: str) -> str:
    return (
        "你是用户的『规划搭档』。下面是你和用户对某一步骤的讨论记录。请综合讨论,"
        "产出『修订后』的这一步。\n"
        f"要修订的步骤原标题: {step_title}\n"
        "只输出 JSON, 结构严格如下, 不要任何解释文字:\n"
        '{"steps":[{"title":"<这里写修订后的真实步骤标题, 可沿用原标题或改写>","detail":"<修订后的说明, 1-3句, 必须包含讨论中敲定的具体数字/预算/期限/条件>","substeps":[{"title":"<子步骤>","detail":""}]}]}\n'
        "注意: 尖括号里是占位示例, 必须替换成真实内容, 不要原样输出占位文字;"
        "保留可操作性; 没有子步骤时 substeps 为空数组。"
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
    mode = body.get("mode", "exec")
    pc = body.get("plan_context")
    system = _plan_system(body.get("goal", ""), step, body.get("step_detail", ""), pc) if mode == "discuss" else _chat_system(body.get("goal", ""), step, body.get("step_detail", ""), pc)
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
    mode = body.get("mode", "exec")
    pc = body.get("plan_context")
    system = _plan_system(body.get("goal", ""), step, body.get("step_detail", ""), pc) if mode == "discuss" else _chat_system(body.get("goal", ""), step, body.get("step_detail", ""), pc)

    async def gen():
        async for chunk in _stream_provider(cfg, system, user_msg, history=body.get("history") or []):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/refine")
async def refine(req: Request):
    """根据与用户的讨论记录，产出修订后的某一步（标题/说明/子步骤）。"""
    body = await req.json()
    step = (body.get("step") or "").strip()
    if not step:
        return JSONResponse({"error": "缺少步骤"}, status_code=400)
    cfg = body.get("provider") or {}
    if not cfg.get("base") or not cfg.get("model"):
        return JSONResponse({"error": "请先在设置里选 AI 接口"}, status_code=400)
    history = body.get("history") or []
    system = _refine_system(step)
    user_msg = "根据以上讨论，请给出修订后的这一步（只输出 JSON）。"
    try:
        text = await _call_provider(cfg, system, user_msg, history=history)
        data = parse_steps(text)
        if not data["steps"]:
            return JSONResponse({"error": "AI 没能产出修订结果，换个说法再试"}, status_code=502)
        s = data["steps"][0]
        return {"title": s["title"], "detail": s["detail"], "substeps": s["substeps"]}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"AI 调用失败: {e}"}, status_code=500)


# ---------- 计划存储（服务端持久化） ----------
# 为什么不用 localStorage: 桌面 App 每次启动端口可能变化, 浏览器存储按"地址+端口"隔离,
# 端口一换 localStorage 全部失效 -> 用户感觉"没有记忆"。服务端文件与端口无关。
import threading as _th

DATA_DIR = os.path.join(LOCAL_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
_state_lock = _th.Lock()


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("plans"), list):
            return st
    except Exception:
        pass
    return {"plans": []}


def _save_state(st: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _find_plan(st: dict, pid: str) -> Optional[dict]:
    return next((x for x in st["plans"] if x.get("id") == pid), None)


def _plan_summary(p: dict) -> dict:
    steps = p.get("steps") or []
    done = set(p.get("done") or [])
    n_done = sum(1 for s in steps if s.get("title") in done)
    return {
        "id": p["id"], "goal": p.get("goal", ""), "status": p.get("status", "active"),
        "createdAt": p.get("createdAt"), "updatedAt": p.get("updatedAt"),
        "steps": len(steps), "done": n_done, "chats": len(p.get("chats") or {}),
    }


@app.get("/api/plans")
async def plans_list():
    """计划列表（摘要，按更新时间倒序）。"""
    with _state_lock:
        st = _load_state()
    sums = [_plan_summary(p) for p in st["plans"]]
    sums.sort(key=lambda x: x.get("updatedAt") or 0, reverse=True)
    return {"plans": sums}


@app.post("/api/plans")
async def plans_upsert(req: Request):
    """创建或更新一份完整计划（含步骤、聊天记录、完成状态）。"""
    body = await req.json()
    goal = (body.get("goal") or "").strip()
    steps = body.get("steps")
    if not goal or not isinstance(steps, list):
        return JSONResponse({"error": "goal 和 steps 不能为空"}, status_code=400)
    now = int(time.time())
    with _state_lock:
        st = _load_state()
        pid = body.get("id")
        p = _find_plan(st, pid) if pid else None
        if p is None:
            pid = uuid.uuid4().hex[:12]
            p = {"id": pid, "createdAt": now}
            st["plans"].append(p)
        p.update({
            "goal": goal[:200],
            "steps": steps[:60],
            # 部分更新时缺省字段保留原值，避免误清聊天/完成记录
            "chats": body["chats"] if isinstance(body.get("chats"), dict) else p.get("chats", {}),
            "done": body["done"] if isinstance(body.get("done"), list) else p.get("done", []),
            "status": body.get("status") or p.get("status") or "active",
            "updatedAt": now,
        })
        # 容量保护：最多 50 份，超出先删最旧的归档件
        if len(st["plans"]) > 50:
            st["plans"].sort(key=lambda x: (x.get("status") != "archived", -(x.get("updatedAt") or 0)))
            del st["plans"][50:]
        _save_state(st)
    return {"id": pid, "updatedAt": now}


@app.get("/api/plans/{pid}")
async def plans_get(pid: str):
    with _state_lock:
        st = _load_state()
    p = _find_plan(st, pid)
    if not p:
        return JSONResponse({"error": "计划不存在"}, status_code=404)
    return p


@app.post("/api/plans/{pid}/status")
async def plans_status(pid: str, req: Request):
    """归档 / 恢复（status: active | archived）。"""
    body = await req.json()
    status = body.get("status")
    if status not in ("active", "archived"):
        return JSONResponse({"error": "status 必须是 active 或 archived"}, status_code=400)
    now = int(time.time())
    with _state_lock:
        st = _load_state()
        p = _find_plan(st, pid)
        if not p:
            return JSONResponse({"error": "计划不存在"}, status_code=404)
        p["status"] = status
        p["updatedAt"] = now
        _save_state(st)
    return {"ok": True}


@app.delete("/api/plans/{pid}")
async def plans_delete(pid: str):
    with _state_lock:
        st = _load_state()
        before = len(st["plans"])
        st["plans"] = [x for x in st["plans"] if x.get("id") != pid]
        if len(st["plans"]) == before:
            return JSONResponse({"error": "计划不存在"}, status_code=404)
        _save_state(st)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(LOCAL_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
