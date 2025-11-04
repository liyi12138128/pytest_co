# code_assistant_tui.py
from __future__ import annotations
import os, re, subprocess, hashlib
from pathlib import Path
from typing import List, Tuple, Optional
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# LLM 配置：自动同步 OPENONION_API_KEY -> CONNECTONION_API_KEY
# ============================================================

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENONION_KEY = os.getenv("OPENONION_API_KEY")
CONNECTONION_KEY = os.getenv("CONNECTONION_API_KEY")

# 如果只配了 OPENONION_API_KEY，就顺手同步给 CONNECTONION_API_KEY
if not CONNECTONION_KEY and OPENONION_KEY:
    os.environ["CONNECTONION_API_KEY"] = OPENONION_KEY
    CONNECTONION_KEY = OPENONION_KEY

# 是否启用 LLM：任意一个 key 存在即视为可用
USE_LLM = bool(OPENAI_KEY or OPENONION_KEY or CONNECTONION_KEY)

# 模型选择逻辑：
# - 有 OPENAI_KEY：默认 gpt-4o-mini
# - 否则（只有 OpenOnion）：默认 co/o4-mini（和 auto_test_worker 同风格）
if OPENAI_KEY:
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
else:
    LLM_MODEL = os.getenv("LLM_MODEL", "co/o4-mini")

if USE_LLM:
    from connectonion import llm_do
else:
    print("[TUI] LLM disabled: no OPENAI_API_KEY / OPENONION_API_KEY / CONNECTONION_API_KEY found.")

# ============================================================
# Textual / TUI 相关
# ============================================================

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static, Input
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button  # if you later add buttons to the layout

ROOT = Path.cwd()
SRC_DIR, TEST_DIR, DOC_DIR = ROOT / "src", ROOT / "tests", ROOT / "docs"

# 为 TUI 搞一个简易缓存目录，用源码哈希存 SPEC
CACHE_DIR = ROOT / ".tui_cache"
CACHE_DIR.mkdir(exist_ok=True)

FENCE_HEAD = re.compile(r"^\s*```(?:\w+)?\s*\n", re.DOTALL)
FENCE_TAIL = re.compile(r"\n\s*```\s*$", re.DOTALL)


def strip_fences(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = FENCE_HEAD.sub("", s.strip())
    s = FENCE_TAIL.sub("", s)
    return s.strip()


def _code_hash(s: str) -> str:
    """对源码做 SHA256，用于 SPEC 缓存 key。"""
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _cache_get(kind: str, h: str) -> Optional[str]:
    """从 .tui_cache 读缓存（例如 kind='spec'）"""
    p = CACHE_DIR / f"{kind}_{h}.txt"
    if p.exists():
        return p.read_text(encoding="utf-8", errors="ignore")
    return None


def _cache_put(kind: str, h: str, text: str) -> None:
    p = CACHE_DIR / f"{kind}_{h}.txt"
    p.write_text(text, encoding="utf-8")


def list_files(base: Path, exts=(".py",)) -> List[str]:
    if not base.exists():
        return []
    out: List[str] = []
    for p in base.rglob("*"):
        if p.is_file() and p.suffix in exts and p.name != "__init__.py":
            out.append(str(p.relative_to(ROOT)))
    return sorted(out)


def read_text(rel: str, n=4000) -> str:
    p = ROOT / rel
    if not p.exists():
        return "(file not found)"
    t = p.read_text(encoding="utf-8", errors="ignore")
    return t[:n]


def modname_from_path(rel: str) -> str:
    return ".".join(Path(rel).with_suffix("").parts)


def ensure_parent_write(rel: str, content: str):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def render_pytest_skeleton(modname: str) -> str:
    return f"""

import importlib

def _target():
    return importlib.import_module("{modname}")

def test_import():
    mod = _target()
    assert mod is not None
"""


def render_doc_skeleton(modname: str) -> str:
    return f"""# {modname}

## Quick Start
```python
import {modname}
Auto-generated minimal documentation.
"""

# ---------- 行为规格 & 测试生成模板（加了“别乱假设 TypeError”规则） ----------

SPEC_TMPL = """You are a senior Python code analyst.
From the following Python module source code, derive a concise BEHAVIOR SPEC
for PUBLIC APIs only (exclude names starting with '_').

Write short bullet points only. For each function/class, specify:
- purpose and typical inputs/outputs
- boundary conditions and invalid inputs
- expected exceptions

----- SOURCE CODE -----
{code}
----- END -----
"""

TEST_FROM_SPEC_TMPL = """You are a professional Python test engineer.
You will receive:
- the fully-qualified module name
- a behavior SPEC extracted from the source
- the original implementation source code

Write a runnable pytest file for module: {module_qualname}

General rules:
- Output ONLY Python code (no prose, no markdown fences)
- Use: from {module_qualname} import ...
- Test PUBLIC APIs only (no underscore-prefixed names)
- Use pytest.mark.parametrize for normal/boundary/error cases where helpful
- Cover exceptions with pytest.raises when they are part of the intended behavior
- For file/FS operations use tmp_path fixture (no hard-coded paths)
- For env/time/random/subprocess, prefer monkeypatch and seeded randomness
- Deterministic, no network, no real sleeps
- Avoid trivial tests that only assert constant equality; assert behavior/invariants
- If the module has no public callable APIs, at least test that import succeeds.
- Do NOT modify sys.path or PYTHONPATH in the test file.

Exception & type rules (VERY IMPORTANT):
- Do NOT assume functions raise TypeError or ValueError for “wrong” types
  unless EITHER:
  * the behavior SPEC explicitly says so, OR
  * the implementation clearly does explicit type checking and raises those errors.
- If the implementation simply delegates to Python’s built-in behavior
  (e.g. arithmetic on arbitrary operands), you SHOULD:
  * focus on realistic, documented use cases, and
  * avoid contrived “wrong type” tests that contradict the actual behavior
    (e.g. in Python, 5 * "x" is "xxxxx", not a TypeError).
- When in doubt, prefer not to test overly strict type expectations.

You will now see the behavior SPEC and the implementation source.

----- BEHAVIOR SPEC -----
{spec}
----- END SPEC -----

----- IMPLEMENTATION SOURCE -----
{code}
----- END IMPLEMENTATION SOURCE -----
"""


def polish_test_code(code: str, module_qualname: str) -> str:
    """
    清理 LLM 生成的测试里的路径 hack，并强制使用 fully-qualified import。
    """
    # 去掉 sys.path / Path 相关 hack
    code = re.sub(r"(?m)^\s*import\s+sys\s*$", "", code)
    code = re.sub(r"(?m)^\s*from\s+pathlib\s+import\s+Path\s*$", "", code)
    code = re.sub(r"(?m)^\s*sys\.path\[[^\n]*\].*$", "", code)
    code = re.sub(r"(?m)^\s*sys\.path\.insert\([^\n]*\)\s*$", "", code)

    base = module_qualname.split(".")[-1]

    # 行首的导入替换
    code = re.sub(
        rf"(?m)^\s*from\s+{re.escape(base)}\s+import\s+",
        f"from {module_qualname} import ",
        code,
    )
    code = re.sub(
        rf"(?m)^\s*import\s+{re.escape(base)}\s+as\s+(\w+)\s*$",
        rf"import {module_qualname} as \1",
        code,
    )
    code = re.sub(
        rf"(?m)^\s*import\s+{re.escape(base)}\s*$",
        f"import {module_qualname}",
        code,
    )

    # 非行首场景下的替换（防止漏网之鱼）
    code = re.sub(
        rf"\bfrom\s+{re.escape(base)}\s+import\s+",
        f"from {module_qualname} import ",
        code,
    )
    code = re.sub(
        rf"\bimport\s+{re.escape(base)}\b",
        f"import {module_qualname}",
        code,
    )

    # 如果测试里完全没出现 module_qualname，就兜底加一行通配导入
    if module_qualname not in code:
        code = f"from {module_qualname} import *\n\n" + code

    return code


# ---------- Generators ----------


def gen_test(rel: str) -> Tuple[str, str]:
    """
    流程：
    1) 源码 → SPEC（带缓存）
    2) 用 SPEC + 实现源码 生成 pytest
    3) 若 SPEC 路线失败，再用老的“直接从源码生成测试”提示词再试一次（也有异常规则）
    4) 仍失败则退回 skeleton
    """
    src = read_text(rel)
    mod = modname_from_path(rel)

    # 没启用 LLM：直接 skeleton
    if not USE_LLM:
        print("[gen_test] USE_LLM = False，直接使用 skeleton 测试。")
        code = render_pytest_skeleton(mod)
        base = Path(rel).with_suffix("").name
        return f"tests/test_{base}.py", code

    # 1) 对源码做 hash，用于 SPEC 缓存
    h = _code_hash(src)

    # 2) SPEC：先看缓存，没有就调用 LLM
    spec_text = _cache_get("spec", h)
    if not spec_text:
        try:
            spec_prompt = SPEC_TMPL.format(code=src)
            spec_text = llm_do(spec_prompt, model=LLM_MODEL)
            spec_text = strip_fences(spec_text or "")
        except Exception as e:
            print("[gen_test] LLM 生成 SPEC 出错:", repr(e))
            spec_text = ""
        if spec_text.strip():
            _cache_put("spec", h, spec_text)

    # 3) 路线一：根据 SPEC + 源码 生成 pytest 测试
    code = ""
    try:
        test_prompt = TEST_FROM_SPEC_TMPL.format(
            module_qualname=mod,
            spec=spec_text or "(no spec available, infer from source)",
            code=src,
        )
        code = llm_do(test_prompt, model=LLM_MODEL)
        code = strip_fences(code or "")
    except Exception as e:
        print("[gen_test] LLM 通过 SPEC 生成测试出错:", repr(e))
        code = ""

    # 4) 如果 SPEC 路线失败，走老的源码→测试提示词再试一次（也带异常规则）
    if not code.strip():
        try:
            legacy_prompt = (
                f"You are a professional Python test engineer.\n"
                f"Generate a robust pytest module for `{mod}`.\n"
                f"- Import correctly (from {mod} import ...)\n"
                f"- Cover public functions/classes with normal and boundary cases\n"
                f"- For error cases, only expect TypeError/ValueError if the code explicitly raises them\n"
                f"- Do NOT assume stricter type checks than the implementation actually performs\n"
                f"- Use pytest.mark.parametrize and pytest.raises where appropriate\n"
                f"- No external IO/network; use tmp_path/monkeypatch if needed\n"
                f"- Deterministic, no real sleeps.\n"
                f"Return ONLY Python code (no markdown fences).\n\n"
                f"Source:\n```python\n{src}\n```"
            )
            code = llm_do(legacy_prompt, model=LLM_MODEL)
            code = strip_fences(code or "")
        except Exception as e:
            print("[gen_test] LLM 通过源码直接生成测试出错:", repr(e))
            code = ""

    # 5) 如果两条路线都失败，兜底 skeleton
    if not code.strip():
        print("[gen_test] 两条 LLM 路线都失败，退回 skeleton。")
        code = render_pytest_skeleton(mod)
    else:
        # 6) 清理 sys.path hack，强制用 fully-qualified import
        try:
            code = polish_test_code(code, mod)
        except Exception as e:
            print("[gen_test] polish_test_code 出错，保留原始测试代码。错误:", repr(e))

    base = Path(rel).with_suffix("").name
    return f"tests/test_{base}.py", code


def gen_doc(rel: str) -> Tuple[str, str]:
    """
    文档生成：保持和你一开始类似，但现在如果出错会在终端打印错误。
    """
    src = read_text(rel)
    mod = modname_from_path(rel)

    if USE_LLM:
        try:
            md = llm_do(
                (
                    f"Write concise Markdown docs for `{mod}` (no fences).\n"
                    f"- Title & description\n"
                    f"- Quick start\n"
                    f"- API overview\n\n"
                    f"Source:\n```python\n{src}\n```"
                ),
                model=LLM_MODEL,
            )
            md = strip_fences(md or "")
        except Exception as e:
            print("[gen_doc] LLM 生成文档出错，使用 skeleton。错误:", repr(e))
            md = render_doc_skeleton(mod)
    else:
        md = render_doc_skeleton(mod)

    base = Path(rel).with_suffix("").name
    return f"docs/{base}.md", md


# ---------- Test runner & Git ops ----------


def run_pytest(target: str | None = None) -> Tuple[bool, str]:
    args = ["pytest", "-q"]
    if target:
        args.append(target)

    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, out


def git_apply_patch(patch_text: str) -> Tuple[bool, str]:
    p = subprocess.Popen(
        ["git", "apply", "--index", "-p0"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = p.communicate(patch_text)
    return p.returncode == 0, (out or "") + (err or "")


def git_commit_push(branch: str = "auto/tests-docs") -> str:
    subprocess.run(["git", "checkout", "-B", branch], cwd=ROOT)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    )

    if status.stdout.strip():
        subprocess.run(["git", "add", "-A"], cwd=ROOT)
        subprocess.run(
            ["git", "commit", "-m", "chore: auto-generate tests & docs"], cwd=ROOT
        )

    push = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return (push.stdout or "") + (push.stderr or "")


# ---------- Patch proposal ----------


def propose_patch_pytest_failure(pytest_tail: str) -> str:
    """Ask the LLM to create a unified-diff patch for failing tests."""
    if not USE_LLM:
        return ""

    repo_files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout

    prompt = (
        "Provide ONLY a valid unified diff patch that fixes the failing tests.\n"
        "Rules:\n"
        "1) Output ONLY patch text (git apply --index -p0 compatible), no prose, no fences.\n"
        "2) Use correct repo-relative paths.\n"
        "3) Keep minimal changes.\n\n"
        f"Repository files:\n```\n{repo_files}\n```\n\n"
        f"Failure summary (tail):\n```\n{pytest_tail}\n```"
    )

    try:
        patch = llm_do(prompt, model=LLM_MODEL)
        return strip_fences(patch or "")
    except Exception as e:
        print("[propose_patch] LLM 生成 patch 出错:", repr(e))
        return ""


# ---------- UI Components ----------


class Chat(Static):
    def __init__(self, *args, **kwargs):
        # 初始化 Static 内容为空字符串
        super().__init__("", *args, **kwargs)
        self._buf: list[str] = []  # 用于保存所有聊天内容

    def append(self, who: str, text: str) -> None:
        # 追加消息
        self._buf.append(f"[{who}] {text}")
        # 更新到界面
        self.update("\n".join(self._buf))


class CodeView(Static):
    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)

    def show_text(self, label: str, text: str) -> None:
        self.update(f"=== {label} ===\n{text}")


class IDEApp(App):
    """A Claude-Code-style TUI for AI-assisted coding and testing."""

    # 快捷退出按键
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
    ]

    CSS = """
    Screen { layout: horizontal; }
    #left   { width: 34%; border: solid #444; }
    #mid    { width: 33%; border: solid #444; }
    #right  { width: 33%; border: solid #444; }
    #files  { height: 85%; }
    #code   { height: 85%; padding: 1; overflow: auto; }
    #chat   { height: 85%; padding: 1; overflow: auto; }
    """
    current_file = reactive("", init=False)
    last_pytest_output = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                yield Static(
                    "Files in src/ (↑↓ to select, Enter to open)", id="title_l"
                )
                tbl = DataTable(id="files")
                tbl.add_columns("Path")
                yield tbl
                yield Static(
                    "Keys: g=gen t=pytest d=diff y=apply p=push /=chat Esc=back q=quit",
                    id="help",
                )
            with Vertical(id="mid"):
                cv = CodeView(id="code")
                yield cv
            with Vertical(id="right"):
                yield Static("Chat (/ to focus, Enter to send)", id="title_r")
                yield Chat(id="chat")
                yield Input(
                    placeholder="Ask or command the agent", id="input"
                )
        yield Footer()

    def on_mount(self):
        self.table: DataTable = self.query_one("#files", DataTable)
        self.code: CodeView = self.query_one("#code", CodeView)
        self.chat: Chat = self.query_one("#chat", Chat)
        self.input: Input = self.query_one("#input", Input)
        self.refresh_files()
        self.chat.append(
            "Agent",
            "Hi! Select a file (Enter), press g to generate tests/docs, then t to run pytest.",
        )

    def refresh_files(self):
        self.table.clear()
        files = list_files(SRC_DIR)
        if not files:
            self.table.add_row("(empty) put .py files under src/")
        else:
            for f in files:
                self.table.add_row(f)
        self.table.cursor_type = "row"
        self.table.focus()

    async def on_key(self, event):
        k = event.key.lower()
        if k == "/":
            self.input.focus()
        elif k == "escape":
            self.table.focus()
        elif k == "enter" and self.table.has_focus:
            row = self.table.cursor_row
            if row is None:
                return
            rel = self.table.get_row_at(row)[0]
            if rel.startswith("("):
                return
            self.open_file(rel)
        elif k == "g":
            await self.cmd_generate()
        elif k == "t":
            await self.cmd_test()
        elif k == "d":
            await self.cmd_propose_patch()
        elif k == "y":
            await self.cmd_apply_patch()
        elif k == "p":
            await self.cmd_commit_push()
        # 退出键 q / ctrl+c 由 BINDINGS → action_quit 接管

    def open_file(self, rel: str):
        self.current_file = rel
        src = read_text(rel)
        self.code.show_text(rel, src)
        self.chat.append("Agent", f"Opened {rel}")

    async def cmd_generate(self):
        if not self.current_file:
            self.chat.append("Agent", "Select a file first.")
            return
        tpath, tcode = gen_test(self.current_file)
        ensure_parent_write(tpath, tcode)
        dpath, dmd = gen_doc(self.current_file)
        ensure_parent_write(dpath, dmd)
        self.chat.append("Agent", f"Generated {tpath} and {dpath}")

    async def cmd_test(self):
        ok, out = run_pytest()
        self.last_pytest_output = out
        if ok:
            self.chat.append("Agent", "✅ All tests passed.")
        else:
            tail = "\n".join(out.splitlines()[-40:])
            self.code.show_text("pytest tail (failures)", tail)
            self.chat.append(
                "Agent",
                "❌ Tests failed. Tail shown in middle panel. Press d to propose a patch.",
            )

    async def cmd_propose_patch(self):
        if not self.last_pytest_output:
            self.chat.append("Agent", "Run pytest (t) first.")
            return
        tail = "\n".join(self.last_pytest_output.splitlines()[-120:])
        if not USE_LLM:
            self.chat.append(
                "Agent", "LLM disabled (no API key). Cannot auto-fix."
            )
            return
        self.chat.append(
            "Agent", "Analyzing failures, generating patch..."
        )
        patch = propose_patch_pytest_failure(tail)
        if not patch.strip() or "diff --git" not in patch:
            self.chat.append("Agent", "No valid patch generated.")
            return
        self.code.show_text("Proposed patch", patch)
        (ROOT / ".last_patch.diff").write_text(
            patch, encoding="utf-8"
        )
        self.chat.append(
            "Agent", "Patch displayed in center. Press y to apply it."
        )

    async def cmd_apply_patch(self):
        diff_file = ROOT / ".last_patch.diff"
        if not diff_file.exists():
            self.chat.append(
                "Agent", "No patch found (.last_patch.diff missing)."
            )
            return
        patch = diff_file.read_text(encoding="utf-8")
        ok, msg = git_apply_patch(patch)
        if ok:
            self.chat.append(
                "Agent", "✅ Patch applied. Run pytest again (t)."
            )
        else:
            self.chat.append("Agent", "❌ Patch failed:\n" + msg)

    async def cmd_commit_push(self):
        out = git_commit_push("auto/tests-docs")
        self.chat.append("Agent", "Git push output:\n" + out)


if __name__ == "__main__":
    if not SRC_DIR.exists():
        print("⚠️ src/ folder not found. Run from project root.")
        raise SystemExit(1)
    IDEApp().run()
