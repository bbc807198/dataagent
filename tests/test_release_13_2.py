"""阶段 13.2：发布前定点修复的回归测试（测试安全 / CLI Mock / 配置组合 / 文档一致性）。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(ROOT / "src"))

from web import service  # noqa: E402


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "scripts" / "demo_agent.py"), *args],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=300)


# ---------------------------------------------------------------- P0-1 测试安全
class TestTestSafety:
    def test_no_bare_real_run_question(self):
        """P0-1：测试文件中不得存在未隔离的 real 模式调用。"""
        risky = []
        for p in (ROOT / "tests").glob("*.py"):
            text = p.read_text(encoding="utf-8")
            # 允许的写法：delenv/setenv + monkeypatch，或注入 llm_client
            for m in re.finditer(r'run_question\([^)]*"real"', text, re.S):
                chunk = text[max(0, m.start() - 600):m.end() + 200]
                if "monkeypatch" not in chunk and "setattr" not in chunk:
                    risky.append((p.name, chunk.count("\n")))
        assert not risky, f"存在未隔离的真实模式调用: {risky}"

    def test_fake_key_connection_attempts_all_blocked(self, monkeypatch, net_guard):
        """P0-1：假 Key 场景下，**每一次**连接尝试都被阻断（可观测计数）。

        断言不只是"最终失败"，而是：
        - 若发生尝试 → 必须全部被 net_guard 拦截（blocked == attempts）；
        - 且不得产生任何报告。
        """
        monkeypatch.setenv("DATAAGENT_LLM_API_KEY", "sk-test-fake-000-not-real")

        out = service.run_question("随便一个任意问题", "real", "synthetic")

        assert out.ok is False and out.report_text == ""
        assert out.status != "mock_scenario_unknown"
        # 可观测：尝试次数与拦截次数可比较；不存在"绕过 guard 的真实连接"
        assert net_guard.all_blocked, f"存在未被拦截的尝试: {net_guard.attempts}"
        assert net_guard.blocked == len(net_guard.attempts)

    def test_fake_key_never_constructs_real_sdk_client(self, monkeypatch):
        """P0-1：假 Key 下不得构造**真实** OpenAI 客户端（用记录桩验证调用链）。"""
        monkeypatch.setenv("DATAAGENT_LLM_API_KEY", "sk-test-fake-000-not-real")
        import openai

        constructed: list[dict] = []

        class _RecordingStub:
            def __init__(self, **kwargs):
                constructed.append({k: ("***" if "key" in k.lower() else v)
                                    for k, v in kwargs.items()})
                self.chat = type("C", (), {})()
                self.chat.completions = self

            def create(self, **kwargs):
                raise AssertionError("net_guard: 桩客户端不得真正请求")

        monkeypatch.setattr(openai, "OpenAI", _RecordingStub)
        out = service.run_question("随便一个任意问题", "real", "synthetic")

        assert out.ok is False and out.report_text == ""
        # 证明代码确实走了真实模式（尝试建客户端），但拿到的是桩
        assert constructed, "未走到真实模式分支"
        assert all("***" == v for d in constructed for k, v in d.items()
                   if "key" in k.lower()), "参数中的 Key 不应被记录为明文"

    def test_ci_has_no_api_key(self):
        """CI 配置中不得注入真实 Key。"""
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "DATAAGENT_LLM_API_KEY: \"\"" in ci or "DATAAGENT_LLM_API_KEY" not in ci
        assert "secrets." not in ci


# ---------------------------------------------------------------- P0-2 CLI Mock
class TestCliMock:
    def test_exact_example_succeeds(self):
        """P0-2：完整示例 → 命中场景并成功。"""
        r = run_cli("--question", "帮我分析申购和赎回金额最近的趋势。")
        assert r.returncode == 0
        assert "[Mock 场景]" in r.stdout
        assert "report_generated" in r.stdout

    def test_arbitrary_question_rejected(self):
        """P0-2：任意问题 → 明确 Mock 限制，退出码 2，不生成报告。"""
        r = run_cli("--question", "帮我看看最近资金怎么样")
        assert r.returncode == 2
        assert "[Mock 限制]" in r.stdout
        # 未匹配时只打印场景清单（含"预期 report_generated"字样），
        # 但**不得**出现运行结果行
        assert "状态：report_generated" not in r.stdout
        assert "生成" not in r.stdout.split("[Mock 限制]")[-1].split("生成的")[0]
        # 不泄露本机绝对路径
        assert r"C:\Users" not in r.stdout and "C:/Users" not in r.stdout

    def test_partial_match_rejected(self):
        """P0-2：片段/追加内容不命中。"""
        for q in ("申购", "趋势", "帮我分析申购和赎回金额最近的趋势，并给出建议"):
            r = run_cli("--question", q)
            assert r.returncode == 2, q
            assert "[Mock 限制]" in r.stdout

    def test_real_mode_allows_freeform(self):
        """P0-2：Real 模式不套用 Mock 目录（无 Key 时给配置提示）。"""
        import os
        env = {**os.environ}
        env.pop("DATAAGENT_LLM_API_KEY", None)
        r = subprocess.run([PY, str(ROOT / "scripts" / "demo_agent.py"),
                            "--real", "--question", "任意自然语言问题"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           timeout=300, env=env)
        out = r.stdout + r.stderr
        assert "[Mock 限制]" not in out
        assert "[Mock 场景]" not in out

    def test_all_eight_scenarios_run(self):
        """P0/P1：8 个内置场景全部按预期状态运行。"""
        r = run_cli()
        assert r.returncode == 0
        catalog = service.mock_catalog()
        assert len(catalog) == 8
        for c in catalog:
            assert c["expected"] in r.stdout, f"{c['case_id']} 未出现预期状态"


# ---------------------------------------------------------------- P1-1 配置组合
class TestConfigMismatch:
    def test_mock_project_db_blocked(self):
        """P1-1：Mock + Project DB 被阻止，且不生成报告。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "project_db")
        assert out.ok is False
        assert out.status == "mock_project_db_mismatch"
        assert out.report_text == ""
        assert "配置不匹配" in out.status_text

    def test_mock_synthetic_still_works(self):
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        assert out.ok is True

    def test_cli_rejects_mock_project_db(self):
        r = run_cli("--project-db")
        assert r.returncode == 2
        assert "配置不匹配" in r.stdout

    def test_cli_rejects_mock_with_db_flag(self, tmp_path):
        """任务二回归：`--db PATH` 同样进入 project_db，必须被拒绝。

        注意：不能依赖项目真实库是否存在——用临时空 DuckDB 文件即可。
        """
        from dataagent import db as _db
        db_file = tmp_path / "any.duckdb"
        con = _db.connect(db_file)
        con.close()
        assert db_file.is_file()

        r = run_cli("--db", str(db_file))
        assert r.returncode == 2, r.stdout
        assert "配置不匹配" in r.stdout
        assert "--db <路径>" in r.stdout          # 提示应指出实际来源
        assert "report_generated" not in r.stdout

    def test_cli_db_flag_blocked_before_any_query(self, tmp_path):
        """任务二：阻断发生在建库/查询之前（不得改动指定数据库）。"""
        from dataagent import db as _db
        db_file = tmp_path / "untouched.duckdb"
        con = _db.connect(db_file)
        con.close()
        before = db_file.read_bytes()

        r = run_cli("--db", str(db_file), "--question",
                    "帮我分析申购和赎回金额最近的趋势。")
        assert r.returncode == 2
        assert db_file.read_bytes() == before    # 数据库未被修改

    def test_no_silent_date_rewrite(self):
        """P1-1：不得把 2024 计划日期偷偷改成本库日期。"""
        src = (ROOT / "web" / "service.py").read_text(encoding="utf-8")
        assert "mock_project_db_mismatch" in src
        # 阻断分支应在构建 config 之前返回
        idx_block = src.find("mock_project_db_mismatch")
        idx_cfg = src.find("cfg = build_config")
        assert 0 < idx_block < idx_cfg


# ---------------------------------------------------------------- P1-2 文档一致性
class TestDocsConsistency:
    def test_readme_no_stale_web_claim(self):
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "没有 Web 界面" not in txt
        assert "仅命令行" not in txt
        assert "streamlit run app.py" in txt

    def test_readme_real_command_uses_question(self):
        """真实模型示例必须带 --question（避免整批调用）。"""
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        for line in txt.splitlines():
            if "demo_agent.py --real" in line and "--question" not in line \
                    and "全部 8 个内置场景" not in line and "会依次" not in line:
                pytest.fail(f"README 存在整批调用的示例命令: {line.strip()}")
        assert "会依次运行**全部 8 个内置场景**" in txt

    def test_example_questions_removed(self):
        """P1-2：重复的 EXAMPLE_QUESTIONS 已删除，示例从 DEMOS 派生。"""
        assert not hasattr(service, "EXAMPLE_QUESTIONS")
        assert len(service.mock_catalog()) == 8
        kinds = [c["kind"] for c in service.mock_catalog()]
        assert kinds.count("success") == 5 and kinds.count("boundary") == 3

    def test_guides_match_catalog(self):
        """8 个场景表出现在 README 与 Web 指南中，且与 DEMOS 一致。"""
        for rel in ("README.md", "docs/web_user_guide.md"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            for c in service.mock_catalog():
                assert c["question"] in txt, f"{rel} 缺少场景 {c['case_id']}"

    def test_powershell_key_compat(self):
        """PowerShell 片段不得使用仅 PS7 支持的 -AsPlainText。"""
        for rel in ("docs/web_user_guide.md", "docs/real_web_acceptance.md"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            assert "-AsPlainText" not in txt, rel

    def test_difference_values(self):
        """P1-3：差额为 6.39 / 7.59 亿元（校验公开验收指南，不依赖内部报告）。"""
        rel = "docs/real_web_acceptance.md"
        txt = (ROOT / rel).read_text(encoding="utf-8")
        assert "6.39" in txt and "7.59" in txt, rel
        assert "≈0.6 亿元" not in txt and "约 0.6 亿" not in txt, rel

    def test_entry_points_exist(self):
        """README 引用的入口文件真实存在。"""
        for rel in ("app.py", "scripts/demo_agent.py", "scripts/evaluate_agent.py",
                    "scripts/verify_golden.py", "scripts/build_database.py"):
            assert (ROOT / rel).is_file(), rel

# ---------------------------------------------------------------- 最终文档校正（发布前）
class TestFinalDocCorrection:
    """发布前文档校正的固化断言（防止过期表述回流）。"""

    def test_readme_scope_mentions_local_web_demo(self):
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "Web 交互界面的场景（当前版本均未实现）" not in txt
        assert "本地单用户 Streamlit" in txt
        # 必须说明没有鉴权/隔离，不得暗示可对外服务
        assert "没有鉴权" in txt or "无鉴权" in txt

    def test_ci_status_phrase(self):
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "CI 流水线" not in txt.split("近期")[-1] or "尚未在 GitHub 远端" in txt
        assert "尚未在 GitHub 远端" in txt

    def test_architecture_limits_not_stale(self):
        txt = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        assert "Web 界面、k-匿名抑制、Benchmark：**未实现**" not in txt
        # 已实现的必须写明
        assert "本地单用户 Streamlit 界面" in txt
        assert "离线 Benchmark" in txt
        # 仍存在的限制必须保留
        for kw in ("k-匿名", "无公网鉴权", "无多用户隔离"):
            assert kw in txt, kw

    def test_cli_usage_does_not_show_rejected_combo(self):
        txt = (ROOT / "scripts" / "demo_agent.py").read_text(encoding="utf-8")
        head = txt.split('"""', 2)[1]
        assert "--project-db        # 合成解析" not in head
        assert "会被拒绝" in head
        assert "--real --project-db" in head

    def test_api_key_uses_secure_input(self):
        """README 与两份指南统一使用 Read-Host -AsSecureString，禁止明文粘贴。"""
        for rel in ("README.md", "docs/user_guide.md", "docs/web_user_guide.md",
                    "docs/real_web_acceptance.md"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            assert "Read-Host -AsSecureString" in txt, rel
            assert 'DATAAGENT_LLM_API_KEY = "你的Key"' not in txt, rel
            assert "DATAAGENT_LLM_API_KEY  = \"你的Key\"" not in txt, rel


class TestNoStaleUnimplementedClaims:
    def test_docs_do_not_claim_web_or_benchmark_missing(self):
        for rel in ("README.md", "docs/architecture.md", "docs/web_user_guide.md",
                    "docs/real_web_acceptance.md"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            for bad in ("Web 界面未实现", "Web 未实现", "Benchmark 未实现",
                        "没有 Web 前端", "仅命令行"):
                assert bad not in txt, f"{rel} 含过期表述: {bad}"

# ---------------------------------------------------------------- 最终文档校正（发布前）
class TestFinalDocCorrection:
    """发布前文档校正的固化断言（防止过期表述回流）。"""

    def test_readme_scope_mentions_local_web_demo(self):
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "Web 交互界面的场景（当前版本均未实现）" not in txt
        assert "本地单用户 Streamlit" in txt
        # 必须说明没有鉴权/隔离，不得暗示可对外服务
        assert "没有鉴权" in txt or "无鉴权" in txt

    def test_ci_status_phrase(self):
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "CI 流水线" not in txt.split("近期")[-1] or "尚未在 GitHub 远端" in txt
        assert "尚未在 GitHub 远端" in txt

    def test_architecture_limits_not_stale(self):
        txt = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        assert "Web 界面、k-匿名抑制、Benchmark：**未实现**" not in txt
        # 已实现的必须写明
        assert "本地单用户 Streamlit 界面" in txt
        assert "离线 Benchmark" in txt
        # 仍存在的限制必须保留
        for kw in ("k-匿名", "无公网鉴权", "无多用户隔离"):
            assert kw in txt, kw

    def test_cli_usage_does_not_show_rejected_combo(self):
        txt = (ROOT / "scripts" / "demo_agent.py").read_text(encoding="utf-8")
        head = txt.split('"""', 2)[1]
        assert "--project-db        # 合成解析" not in head
        assert "会被拒绝" in head
        assert "--real --project-db" in head

    def test_api_key_uses_secure_input(self):
        """README 与两份指南统一使用 Read-Host -AsSecureString，禁止明文粘贴。"""
        for rel in ("README.md", "docs/user_guide.md", "docs/web_user_guide.md",
                    "docs/real_web_acceptance.md"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            assert "Read-Host -AsSecureString" in txt, rel
            assert 'DATAAGENT_LLM_API_KEY = "你的Key"' not in txt, rel
            assert "DATAAGENT_LLM_API_KEY  = \"你的Key\"" not in txt, rel


class TestNoStaleUnimplementedClaims:
    def test_docs_do_not_claim_web_or_benchmark_missing(self):
        for rel in ("README.md", "docs/architecture.md", "docs/web_user_guide.md",
                    "docs/real_web_acceptance.md"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            for bad in ("Web 界面未实现", "Web 未实现", "Benchmark 未实现",
                        "没有 Web 前端", "仅命令行"):
                assert bad not in txt, f"{rel} 含过期表述: {bad}"
