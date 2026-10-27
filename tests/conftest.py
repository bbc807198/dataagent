"""pytest 公共配置与 fixtures。

两条测试路径：
1. 公开合成数据（tests/fixtures/synthetic/）：不依赖本机原始数据，任何克隆仓库的人都能跑。
2. 本地真实数据集成测试（marker: integration）：使用项目内 Purchase Redemption Data/，
   数据缺失时自动跳过（而不是失败）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent import config, db, ingestion, shibor  # noqa: E402

SYNTHETIC_DIR = Path(__file__).parent / "fixtures" / "synthetic"

# 合成数据的定制契约期望（12 行 / 3 用户 / 11 天 / 1 个画像孤儿用户 / 0 恒等式违例）
SYNTHETIC_EXPECTATIONS = {
    "user_balance_table": {
        "rows": 12, "users": 3, "dates": 11,
        "date_min": "2024-01-02", "date_max": "2024-01-12",
        "duplicate_keys": 0, "negative_amount_rows": 0,
    },
    "user_profile_table": {"rows": 4, "duplicate_user_ids": 0},
    "mfd_day_share_interest": {"rows": 11, "duplicate_dates": 0},
    "mfd_bank_shibor": {"rows": 8, "duplicate_dates": 0},
    "joins": {
        "balance_users_not_in_profile": 0,
        "profile_users_not_in_balance": 1,  # 合成数据故意保留一个画像孤儿用户
        "balance_dates_not_in_yield": 0,
    },
    "balance_flow_diagnostic": {"expected_violations": 0},
}


@pytest.fixture(scope="session")
def synthetic_dir() -> Path:
    return SYNTHETIC_DIR


@pytest.fixture()
def synth_db(tmp_path):
    """基于合成 CSV 的临时 DuckDB（每个测试函数独立）。"""
    con = db.connect(tmp_path / "synth.duckdb")
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTHETIC_DIR)
    failed = [r for r in reports if not r.ok]
    assert not failed, f"合成数据导入失败: {[r.error for r in failed]}"
    shibor.create_fill_view(con)
    yield con
    con.close()


@pytest.fixture(scope="session")
def real_db(tmp_path_factory):
    """本地真实数据集成测试数据库（会话级构建一次）。无原始数据时跳过。"""
    if not config.raw_data_available():
        pytest.skip("本地真实原始数据不可用（Purchase Redemption Data/ 缺失），跳过集成测试")
    tmp = tmp_path_factory.mktemp("realdata")
    con = db.connect(tmp / "real.duckdb")
    db.init_schema(con)
    reports = ingestion.import_all(con)
    failed = [r for r in reports if not r.ok]
    assert not failed, f"真实数据导入失败: {[r.error for r in failed]}"
    shibor.create_fill_view(con)
    yield con
    con.close()

@pytest.fixture
def net_guard(monkeypatch):
    """可观测的网络隔离：记录每一次外部连接尝试并阻断。

    用法：
        def test_x(net_guard):
            ...            # 运行被测代码
            assert net_guard.attempts == 0        # 从未尝试
            # 或
            assert net_guard.all_blocked          # 尝试过但全部被阻断

    与"仅断言最终失败状态"不同：本夹具让测试**能区分**
    "没有尝试连接" 与 "尝试了但被拦截"。
    """
    import socket

    class _Guard:
        def __init__(self):
            self.attempts: list[str] = []
            self.blocked: int = 0
            self.real_calls: int = 0

        @property
        def all_blocked(self) -> bool:
            return self.blocked == len(self.attempts)

        @property
        def never_attempted(self) -> bool:
            return not self.attempts

    guard = _Guard()
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def fake_connect(self, address, *a, **k):
        guard.attempts.append(str(address))
        guard.blocked += 1
        raise AssertionError(f"net_guard: 测试禁止外部连接 {address}")

    def fake_create(address, *a, **k):
        guard.attempts.append(str(address))
        guard.blocked += 1
        raise AssertionError(f"net_guard: 测试禁止外部连接 {address}")

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    monkeypatch.setattr(socket, "create_connection", fake_create)
    # 供断言"是否有代码绕过 guard 直接调用原函数"
    guard._real_connect = real_connect
    guard._real_create = real_create
    yield guard
