# -*- coding: utf-8 -*-
"""
学情三方协同智能体 · 本地版

三个角色：
  小诊 —— 学生端，错题诊断
  小析 —— 教师端，班级学情分析
  小通 —— 家长端，学情翻译

用法：双击「启动.bat」，浏览器打开网页即可。
纯 Python 标准库，不需要安装任何依赖。需要 Python 3.9+。
"""

import json
import os
import sys
import time
import webbrowser
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest, error as urlerror

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# ---------------------------------------------------------------------------
# 云端模式
# 部署到服务器时，用环境变量提供配置，并锁死网页上的「设置」入口，
# 避免任何访问者改配置或看到 Key。
# ---------------------------------------------------------------------------
ENV_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
ENV_BASE_URL = os.environ.get("LLM_BASE_URL", "").strip()
ENV_MODEL = os.environ.get("LLM_MODEL", "").strip()
CLOUD = bool(ENV_API_KEY) or bool(os.environ.get("PORT"))

try:
    RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_HOUR", "60"))
except ValueError:
    RATE_LIMIT = 60

_RATE = {}


def rate_ok(ip):
    """简单的每 IP 每小时限流，防止公开链接被刷爆额度。"""
    if RATE_LIMIT <= 0:
        return True
    now = time.time()
    hits = _RATE.setdefault(ip, [])
    hits[:] = [t for t in hits if now - t < 3600]
    if len(hits) >= RATE_LIMIT:
        return False
    hits.append(now)
    return True

DEFAULT_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "port": 8770,
}

MAX_HISTORY = 16
UPSTREAM_TIMEOUT = 300

# ---------------------------------------------------------------------------
# 知识库（内嵌，保证智能体开箱即用）
# ---------------------------------------------------------------------------
KB = """
【初中数学知识点库（人教版，14 条核心知识点）】
K001 有理数的运算 | 七上第一章 | 前置：无 | 常见错误：负负得正记反、去括号不变号、运算顺序混
K002 整式的加减 | 七上第二章 | 前置：有理数运算 | 常见错误：合并同类项时指数也相加、括号前负号只变第一项
K003 一元一次方程 | 七上第三章 | 前置：整式加减 | 常见错误：移项不变号、去分母漏乘不含分母项
K004 二元一次方程组 | 七下第八章 | 前置：一元一次方程 | 常见错误：加减消元符号错、代入漏括号
K005 不等式与不等式组 | 七下第九章 | 前置：一元一次方程 | 常见错误：乘除负数忘记变号、解集端点开闭混淆
K006 全等三角形 | 八上第十二章 | 前置：三角形 | 常见错误：误用 SSA、对应顶点顺序写错
K007 勾股定理 | 八下第十七章 | 前置：全等三角形 | 常见错误：直角边与斜边混淆、未说明斜边就套公式、忘记分类讨论
K008 平行四边形 | 八下第十八章 | 前置：全等三角形 | 常见错误：性质当判定用、对角线互相平分记反
K009 一次函数 | 八下第十九章 | 前置：平面直角坐标系 | 常见错误：k 的符号与增减性对应错、图像平移方向搞反
K010 一元二次方程 | 九上第二十一章 | 前置：一元一次方程 | 常见错误：判别式符号判断错、两边同除含 x 的式子导致丢根
K011 二次函数 | 九上第二十二章 | 前置：一元二次方程 | 常见错误：顶点坐标符号写反、闭区间最值在端点而非顶点
K012 相似三角形 | 九下第二十七章 | 前置：全等三角形 | 常见错误：对应边找错、面积比与相似比混淆（是平方关系）
K013 圆 | 九上第二十四章 | 前置：勾股定理 | 常见错误：圆周角与圆心角关系记错、切线判定少"半径"条件
K014 概率初步 | 九上第二十五章 | 前置：无 | 常见错误：分母漏算、重复计数、放回与不放回混淆

【错因分类体系（A–F）】
A 概念理解：定义混淆、性质误用、取值范围忽略
B 计算失误：符号错误、去括号、移项、运算顺序
C 审题偏差：漏条件、看错问法、单位未统一
D 方法选择：选错解题工具（如该用判别式却硬配方）
E 迁移失败：例题会做，变式就不会
F 表达规范：步骤缺失、书写跳步

【变式题规则】
变式1 改条件：保持知识点和问法不变，只改已知量（数字、图形位置、增减条件），难度与原题持平。
变式2 改问法：保持条件和知识点不变，改变所求（正向求值→反向求条件、求结果→求范围），略难。
变式3 改情境：保持数学结构不变，换成生活或跨学科场景，最难。
要求：难度递增、不得超纲、每题附答案和一句提示（提示不能是完整解法）。
"""

# ---------------------------------------------------------------------------
# 三个角色的系统提示词
# ---------------------------------------------------------------------------
PROMPTS = {
    "xiaozhen": """# 角色
你是「小诊」，一位有 15 年教龄的初中数学老师。你的任务不是给答案，而是帮学生找到"我到底卡在哪一步"。

# 工作流程
1. 识别题目：完整识别题干、已知条件、图形信息、学生的作答痕迹。图片模糊或信息不全时，先请学生补充，绝对不要猜。
2. 定位知识点：从知识库中检索，输出知识点、章节、前置知识。
3. 判断对错：如果学生给了答案，先判断对错；没给答案就只做题目分析。
4. 错因诊断：从错因分类体系里选 1–2 项，必须给出证据（指出哪一步、哪个数字、哪句话暴露了这个错误）。不允许只写"基础不牢"这种空话。
5. 引导思考：给 1–2 个苏格拉底式问题，引导学生自己发现错误。不要直接给完整解法。
6. 变式训练：生成 3 道题，分别改条件、改问法、改情境，难度递增，每题给答案和一句提示。
7. 复习建议：按遗忘曲线给出 1 天后、3 天后、7 天后的复习安排。

# 输出格式
严格按以下结构输出，不要增减标题：

【题目识别】
（一句话复述题目，确认没看错）

【知识点】
知识点：×××
章节：人教版 ×年级×册 第×章
前置知识：×××

【错因诊断】
类型：X · ×××
证据：……（必须引用学生的具体步骤或数字）

【引导问题】
1. ……
2. ……

【变式训练】
变式1（改条件）：……
变式2（改问法）：……
变式3（改情境）：……

【复习建议】
第 1 次：明天，做 ……
第 2 次：3 天后，做 ……
第 3 次：7 天后，做 ……

# 规则
- 不要一上来就给完整答案。
- 学生连续两次说"还是不会"，才给出完整步骤，并加一句"这道题的关键是……"。
- 语言像真人老师，不要说"作为 AI"。
- 不确定的知识点，直接说"这个知识点我不太确定，建议查教材第×章"，不要编造。
- 不评价学生智力，不做比较，不制造焦虑。

# 知识库
""" + KB,

    "xiaoxi": """# 角色
你是「小析」，一位初中数学教研员。你面对的是教师，不是学生。你的输出要能直接拿到教研会上用。

# 输入
老师会给你班级的错题记录，可能是表格、文字列表或 CSV。常见字段：学生、题目、知识点、错因类型、得分。

# 工作流程
1. 解析数据：先说明收到多少条记录、覆盖多少学生、多少知识点。数据缺失时先说明局限。
2. 聚类分析：按知识点统计错误人次和错误率；按错因类型统计分布；交叉分析"哪个知识点 + 哪种错因"最集中。
3. 识别关键薄弱点：找出 3 个最值得干预的点，排序依据是错误人数 × 该知识点的重要性（是否核心考点、是否前置知识）。说明排序理由。
4. 教学建议：针对每个薄弱点给 1 条具体课堂干预动作，必须具体到"课上做什么"，不说"加强练习"这种空话。
5. 分层作业：按 A/B/C 三层给出作业建议。
6. 数据局限：说明样本偏差（样本量、是否只有一次考试、是否覆盖全体）。

# 输出格式
【数据概览】
记录数：N 条 | 学生数：N 人 | 知识点数：N 个

【知识点热力表】
| 知识点 | 错误人数 | 错误率 | 主要错因 |
（按错误率降序）

【关键薄弱点 TOP3】
1. ×××（错误率 ××%，主要错因：××）
   依据：……
2. ……
3. ……

【下节课建议】
1. 针对 ×××：……
2. ……

【分层作业】
A 层：……
B 层：……
C 层：……

【数据局限】
……

# 规则
- 绝对不编造数据。数据里没有的，明确说"数据未覆盖"。
- 建议必须可执行，不说套话。
- 用教研员口吻，专业但不居高临下。
- 数据少于 10 条时，直接提醒"样本过小，结论仅供参考"。

# 知识库
""" + KB,

    "xiaotong": """# 角色
你是「小通」，一位特别会跟家长沟通的初中班主任。你的读者是初中生的家长：不懂教育术语、工作忙、容易焦虑。

# 输入
你会收到学生的学情数据，或者「小诊」的诊断报告。

# 工作流程
1. 翻译术语：把专业表达换成生活化表达。
   - 不说"二次函数掌握度 62%"，说"二次函数这一章，孩子能做对一半多一点"。
   - 不说"迁移能力不足"，说"课本上的例题会做，换个数字或问法就容易卡住"。
2. 先说进步，再说问题：先指出一个具体的、真实的小进步，不能是空泛的"很棒"。
3. 给 3 条家庭可执行动作：每条不超过 15 分钟，家长不需要懂数学也能做，必须是"今天晚上就能做"的。
4. 情绪安抚：数据不好时，明确告诉家长这是这个阶段的常见问题，可以补上。

# 输出格式
【孩子这周的情况】
（2–3 句，客观描述）

【值得肯定的一点】
（必须具体）

【需要关注的一点】
（1 个就够，不要列一堆）

【在家可以做的 3 件小事】
1. ……（≤15 分钟）
2. ……
3. ……

【给家长的一句话】

# 规则
- 绝对不制造焦虑，不排名，不和"别人家的孩子"比较。
- 不承诺"提分""逆袭""保证考上"。
- 每条建议必须具体到动作，不说"多关心孩子"。
- 语言口语化，像发微信，不用书面语。
- 发现学生可能有心理问题时，建议家长联系班主任或学校心理老师，不要自己诊断。

# 知识库
""" + KB,
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)

    # 云端模式：配置一律来自环境变量，不读也不写 config.json
    if CLOUD:
        cfg["api_key"] = ENV_API_KEY
        if ENV_BASE_URL:
            cfg["base_url"] = ENV_BASE_URL
        if ENV_MODEL:
            cfg["model"] = ENV_MODEL
        return cfg

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update(saved)
    except FileNotFoundError:
        save_config(cfg)
    except Exception as e:
        print(f"  [警告] 读取 config.json 失败（{e}）")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [警告] 保存 config.json 失败：{e}")


def update_config(updates):
    if CLOUD:
        return load_config()
    cfg = load_config()
    for key in ("base_url", "model"):
        if isinstance(updates.get(key), str):
            cfg[key] = updates[key].strip()
    if updates.get("clear_key"):
        cfg["api_key"] = ""
    elif isinstance(updates.get("api_key"), str) and updates["api_key"].strip():
        cfg["api_key"] = updates["api_key"].strip()
    if "temperature" in updates:
        try:
            cfg["temperature"] = min(2.0, max(0.0, float(updates["temperature"])))
        except (TypeError, ValueError):
            pass
    save_config(cfg)
    return cfg


def build_endpoint(base_url):
    base = (base_url or "").strip() or DEFAULT_CONFIG["base_url"]
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


# ---------------------------------------------------------------------------
# 错误翻译
# ---------------------------------------------------------------------------
def extract_api_message(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw[:300]
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
        for k in ("message", "msg", "detail"):
            if isinstance(obj.get(k), str) and obj[k]:
                return obj[k]
    return raw[:300]


def friendly_http_error(code, detail):
    hint = {
        400: "请求格式有问题",
        401: "API Key 不正确或已失效，请到「设置」里重新填写",
        402: "账户余额不足",
        403: "这个 Key 没有访问该模型的权限",
        404: "接口地址或模型名不对，请检查「设置」",
        408: "接口响应超时",
        429: "请求太频繁或超出额度，稍等一会儿再试",
        500: "大模型服务端出错了",
        502: "网关错误，稍后重试",
        503: "大模型服务暂时不可用",
        504: "网关超时，稍后重试",
    }.get(code, "接口调用失败")
    msg = extract_api_message(detail)
    return f"{hint}（HTTP {code}）" + (f"\n服务端说：{msg}" if msg else "")


# ---------------------------------------------------------------------------
# 示例数据
# ---------------------------------------------------------------------------
def load_sample():
    path = os.path.join(BASE_DIR, "..", "06_模拟班级错题数据.csv")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except Exception:
        return ("student_id,question,knowledge_id,knowledge_name,error_type,difficulty,score\n"
                "S01,解方程x²-5x+6=0,K010,一元二次方程,B,3,7\n"
                "S02,求二次函数y=-2x²+8x-5的最大值,K011,二次函数,B,4,4\n"
                "S03,直角三角形两直角边为6和8求斜边,K007,勾股定理,C,3,5\n")


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        et, _, _ = sys.exc_info()
        if et in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "XueQingAgent/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_json({"error": "找不到 index.html"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.send_file(INDEX_PATH, "text/html; charset=utf-8")
        elif path == "/api/config":
            cfg = load_config()
            self.send_json({
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "temperature": cfg.get("temperature", 0.3),
                "has_key": bool(cfg.get("api_key")),
                "locked": CLOUD,
            })
        elif path == "/api/sample":
            self.send_json({"csv": load_sample()})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_json({"error": "没有这个地址"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self.read_body()
        if path == "/api/config":
            if CLOUD:
                self.send_json({"error": "演示环境已锁定配置，不能修改。"}, 403)
                return
            try:
                cfg = update_config(body)
            except Exception as e:
                self.send_json({"error": f"保存失败：{e}"}, 500)
                return
            self.send_json({
                "ok": True,
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "temperature": cfg.get("temperature", 0.3),
                "has_key": bool(cfg.get("api_key")),
                "locked": CLOUD,
            })
        elif path == "/api/chat":
            self.handle_chat(body)
        else:
            self.send_json({"error": "没有这个地址"}, 404)

    # ---------------- 聊天 ----------------
    def handle_chat(self, body):
        client_ip = self.client_address[0] if self.client_address else "unknown"
        if not rate_ok(client_ip):
            self.send_json({"error": "请求太频繁，请稍后再试。"}, 429)
            return

        cfg = load_config()
        if not cfg.get("api_key"):
            self.send_json({
                "error": "还没有配置 API Key。请点右上角「设置」，填入你的 API Key。"
            }, 400)
            return

        role = body.get("role")
        if role not in PROMPTS:
            self.send_json({"error": "角色不正确"}, 400)
            return

        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            self.send_json({"error": "没有收到任何对话内容"}, 400)
            return

        images = body.get("images") or []
        if not isinstance(images, list):
            images = []

        messages = []
        for m in raw_messages[-MAX_HISTORY:]:
            if not isinstance(m, dict):
                continue
            r = m.get("role")
            c = m.get("content")
            if r not in ("user", "assistant") or not isinstance(c, str) or not c.strip():
                continue
            if messages and messages[-1]["role"] == r:
                messages[-1]["content"] += "\n\n" + c
            else:
                messages.append({"role": r, "content": c})

        if not messages:
            self.send_json({"error": "没有收到任何对话内容"}, 400)
            return

        # 最后一条用户消息带图片时，改成多模态格式
        if images and messages[-1]["role"] == "user":
            parts = [{"type": "text", "text": messages[-1]["content"]}]
            for img in images[:3]:
                if isinstance(img, str) and img.startswith("data:image/"):
                    parts.append({"type": "image_url", "image_url": {"url": img}})
            messages[-1] = {"role": "user", "content": parts}

        payload = {
            "model": cfg.get("model"),
            "messages": [{"role": "system", "content": PROMPTS[role]}] + messages,
            "stream": True,
            "temperature": cfg.get("temperature", 0.3),
        }

        endpoint = build_endpoint(cfg.get("base_url"))
        req = urlrequest.Request(
            endpoint, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + cfg["api_key"])
        req.add_header("Accept", "text/event-stream")
        req.add_header("User-Agent", "XueQingAgent/1.0")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        state = {"closed": False}

        def emit(obj):
            if state["closed"]:
                return
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
            try:
                self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                state["closed"] = True

        try:
            with urlrequest.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
                for raw_line in resp:
                    if state["closed"]:
                        break
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        obj = json.loads(chunk)
                    except ValueError:
                        continue

                    if isinstance(obj, dict) and obj.get("error"):
                        emit({"error": friendly_http_error(
                            200, json.dumps(obj, ensure_ascii=False))})
                        break

                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("reasoning_content"):
                        emit({"reasoning": delta["reasoning_content"]})
                    if delta.get("content"):
                        emit({"text": delta["content"]})

        except urlerror.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            emit({"error": friendly_http_error(e.code, detail)})
        except urlerror.URLError as e:
            emit({"error": "连不上大模型接口（%s）。请检查网络，以及「设置」里的接口地址。" % e.reason})
        except Exception as e:
            emit({"error": "出错了：%s" % e})
        finally:
            emit({"done": True})
            if not state["closed"]:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def make_server(host, start_port):
    last = None
    for port in range(start_port, start_port + 20):
        try:
            return QuietServer((host, port), Handler), port
        except OSError as e:
            last = e
    raise last


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg = load_config()

    if CLOUD:
        # 部署环境：必须绑 0.0.0.0，端口由平台通过 PORT 指定
        host = "0.0.0.0"
        try:
            port = int(os.environ.get("PORT") or cfg.get("port") or 8770)
        except (TypeError, ValueError):
            port = 8770
        httpd = QuietServer((host, port), Handler)
        url = "http://%s:%d/" % (host, port)
    else:
        try:
            start_port = int(cfg.get("port") or 8770)
        except (TypeError, ValueError):
            start_port = 8770
        httpd, port = make_server("127.0.0.1", start_port)
        url = "http://127.0.0.1:%d/" % port

    print("=" * 56)
    print("   学情三方协同智能体 · %s" % ("云端版" if CLOUD else "本地版"))
    print("=" * 56)
    print("   地址      ： %s" % url)
    print("   模型      ： %s" % cfg.get("model", ""))
    print("   接口地址  ： %s" % cfg.get("base_url", ""))
    print("   API Key   ： %s" % ("已配置" if cfg.get("api_key") else "未配置（在网页右上角「设置」里填写）"))
    if CLOUD:
        print("   配置来源  ：环境变量（网页设置已锁定）")
        print("   限流      ：每 IP 每小时 %d 次" % RATE_LIMIT)
    print("-" * 56)
    print("   三个角色：小诊（学生） / 小析（教师） / 小通（家长）")
    if not CLOUD:
        print("   关闭这个窗口即可结束程序。")
    print()
    sys.stdout.flush()

    if not CLOUD:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n   已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
