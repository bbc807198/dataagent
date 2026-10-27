"""只读查询执行器。

执行顺序（任何一步失败即安全退出，不进入下一步）：

1. SQL 输入检查（非空/字符串）。
2. SQLGlot AST 安全校验（``sql_guard.validate_sql``）。
3. 参数占位符数量与类型校验。
4. 查询资源限制：外层包裹 LIMIT（返回行数上限，不破坏内层 CTE/排序/聚合/
   已有 LIMIT 语义）；连接级线程/内存限制。
5. 在只读连接中执行，并用 ``interrupt`` 实现执行超时取消。
6. 结果检查（列名不得包含 user_id；行数截断标记）与结构化响应。

连接级安全配置（duckdb.connect(config=...)）：
- ``enable_external_access=false``：禁用外部文件/URL 访问（read_csv 等）；
- ``autoinstall_known_extensions=false`` / ``autoload_known_extensions=false``：
  禁止扩展自动安装与加载；
- 只读模式 + 线程/内存上限。

审计：记录规范化 SQL 摘要（sha256 指纹）、引用表、是否通过、耗时、行数、
是否截断、拒绝原因/错误类别；不记录完整参数、用户记录或密钥。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import duckdb
import sqlglot
from sqlglot import exp

from . import config, sql_guard

log = logging.getLogger(__name__)

# 允许绑定的参数类型（位置参数）
ALLOWED_PARAM_TYPES = (int, float, str, bool, type(None), date, datetime)

# 查询资源策略（可配置，但有硬上限）
DEFAULT_MAX_ROWS = 1000
HARD_MAX_ROWS = 10000           # 硬上限：调用方配置不得超过
DEFAULT_TIMEOUT_S = 10.0
HARD_MAX_TIMEOUT_S = 60.0       # 硬上限
MAX_COLUMNS = 200               # 返回列数上限


class QueryStatus:
    OK = "ok"
    REJECTED = "rejected"           # 安全校验拒绝
    INVALID_SQL = "invalid_sql"     # 参数/输入问题（非安全）
    TIMEOUT = "timeout"             # 执行超时被取消
    EXECUTION_ERROR = "execution_error"


@dataclass
class QueryResult:
    query_status: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    error_category: str | None = None
    fingerprint: str | None = None
    tables: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "query_status": self.query_status,
            "columns": self.columns,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "execution_ms": self.execution_ms,
            "warnings": self.warnings,
            "error": self.error,
            "error_category": self.error_category,
            "fingerprint": self.fingerprint,
            "tables": self.tables,
        }


@dataclass
class AuditRecord:
    timestamp: str
    fingerprint: str
    tables: list[str]
    validation_passed: bool
    query_status: str
    execution_ms: float
    row_count: int
    truncated: bool
    rejection_reason: str | None = None
    error_category: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def sql_fingerprint(sql: str) -> str:
    """规范化 SQL 的稳定摘要（不包含原始参数值）。"""
    try:
        normalized = sqlglot.parse_one(sql, dialect=sql_guard.DIALECT).sql(dialect=sql_guard.DIALECT)
    except Exception:
        normalized = " ".join(sql.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class QueryService:
    """只读查询服务：校验通过才执行，执行必过资源限制。"""

    def __init__(
        self,
        db_path=None,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # 构造参数边界：类型错误/非正值直接抛 ValueError（fail-fast，配置错误早失败）；
        # 超过硬上限的参数钳制到硬上限（与行数上限策略一致）。
        if isinstance(max_rows, bool) or not isinstance(max_rows, int):
            raise ValueError(f"max_rows 必须是正整数，收到: {max_rows!r}")
        if max_rows <= 0:
            raise ValueError(f"max_rows 必须是正整数，收到: {max_rows}")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValueError(f"timeout_s 必须是正数，收到: {timeout_s!r}")
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须是正数，收到: {timeout_s}")

        self.db_path = str(db_path) if db_path else str(config.DB_PATH)
        self.max_rows = min(int(max_rows), HARD_MAX_ROWS)
        self.timeout_s = min(float(timeout_s), HARD_MAX_TIMEOUT_S)
        self.audit_log: list[AuditRecord] = []
        # 并发串行化（任务 03.2-E）：DuckDB 连接非线程安全，且连接级 interrupt
        # 会取消该连接上正在执行的查询；用 RLock 保证同一时刻只有一个查询在
        # 连接上执行，从而超时计时器只会取消它自己的查询。
        self._lock = threading.RLock()

        # 只读连接 + 连接级安全配置
        self._con = duckdb.connect(
            self.db_path,
            read_only=True,
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "threads": 2,
                "memory_limit": "1GB",
            },
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._con.close()

    def __enter__(self) -> "QueryService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def security_settings(self) -> dict:
        """返回当前连接实际生效的安全设置（供验证与报告）。"""
        out = {}
        for key in ("enable_external_access", "autoinstall_known_extensions",
                    "autoload_known_extensions"):
            try:
                out[key] = self._con.execute(
                    "SELECT current_setting(?)", [key]).fetchone()[0]
            except Exception as e:  # noqa: BLE001
                out[key] = f"unavailable: {type(e).__name__}"
        return out

    # ------------------------------------------------------------------
    def execute(self, sql: str, params=None) -> QueryResult:
        # 0) 输入类型检查必须位于任何处理（含指纹计算）之前：
        #    不可信输入不得在输入校验之前触发异常（任务 03.2-D）。
        if not isinstance(sql, str) or not sql.strip():
            result = QueryResult(query_status=QueryStatus.INVALID_SQL)
            result.error = "SQL 为空或不是字符串"
            result.error_category = "empty_sql"
            self._record("", [], False, result)
            return result

        # 串行化：同一时刻只有一个查询使用共享连接（任务 03.2-E）
        with self._lock:
            return self._execute_locked(sql, params)

    def _execute_locked(self, sql: str, params=None) -> QueryResult:
        """持锁执行（内部方法）。"""
        fp = sql_fingerprint(sql)
        result = QueryResult(query_status=QueryStatus.OK, fingerprint=fp)

        # 2) AST 安全校验
        validation = sql_guard.validate_sql(sql)
        result.tables = validation.tables
        if not validation.passed:
            result.query_status = QueryStatus.REJECTED
            result.error = "；".join(validation.reasons[:5])
            result.error_category = "security_rejected"
            self._record(fp, validation.tables, False, result,
                         rejection_reason=result.error)
            return result

        # 3) 参数校验（数量与类型）
        try:
            exprs = sqlglot.parse(sql, dialect=sql_guard.DIALECT)
            n_ph = sum(1 for e in exprs if e for node in e.find_all(exp.Placeholder))
        except Exception:  # noqa: BLE001 - 已通过校验，理论不可达
            n_ph = -1
        if params is not None:
            if not isinstance(params, (list, tuple)):
                result.query_status = QueryStatus.INVALID_SQL
                result.error = "参数必须是 list 或 tuple（位置参数）"
                result.error_category = "bad_params_type"
                self._record(fp, validation.tables, True, result)
                return result
            if n_ph >= 0 and len(params) != n_ph:
                result.query_status = QueryStatus.INVALID_SQL
                result.error = f"参数数量不匹配：SQL 需要 {n_ph} 个，提供 {len(params)} 个"
                result.error_category = "param_count_mismatch"
                self._record(fp, validation.tables, True, result)
                return result
            for p in params:
                if not isinstance(p, ALLOWED_PARAM_TYPES):
                    result.query_status = QueryStatus.INVALID_SQL
                    result.error = f"不支持的参数类型: {type(p).__name__}"
                    result.error_category = "bad_param_type"
                    self._record(fp, validation.tables, True, result)
                    return result
        elif n_ph > 0:
            result.query_status = QueryStatus.INVALID_SQL
            result.error = f"SQL 包含 {n_ph} 个参数占位符，但未提供参数"
            result.error_category = "missing_params"
            self._record(fp, validation.tables, True, result)
            return result

        # 4) 返回行数上限：外层包裹 LIMIT（不破坏内层语义）
        wrapped = (
            f"SELECT * FROM ({validation.normalized_sql or sql}) "
            f"AS __dataagent_result LIMIT {self.max_rows + 1}"
        )

        # 5) 执行 + 超时取消
        t0 = time.perf_counter()
        timer = threading.Timer(self.timeout_s, self._safe_interrupt)
        timer.start()
        try:
            cur = self._con.execute(wrapped, list(params) if params else [])
            fetched = cur.fetchall()
            col_names = [d[0] for d in cur.description]
        except duckdb.InterruptException:
            timer.cancel()
            result.query_status = QueryStatus.TIMEOUT
            result.error = f"查询执行超过 {self.timeout_s} 秒，已被取消"
            result.error_category = "timeout"
            result.execution_ms = round((time.perf_counter() - t0) * 1000, 2)
            self._record(fp, validation.tables, True, result)
            return result
        except duckdb.Error as e:
            timer.cancel()
            result.query_status = QueryStatus.EXECUTION_ERROR
            result.error = f"查询执行失败: {type(e).__name__}"
            result.error_category = "duckdb_error"
            result.execution_ms = round((time.perf_counter() - t0) * 1000, 2)
            self._record(fp, validation.tables, True, result)
            return result
        except Exception as e:  # noqa: BLE001
            timer.cancel()
            result.query_status = QueryStatus.EXECUTION_ERROR
            result.error = f"查询执行异常: {type(e).__name__}"
            result.error_category = "unexpected_error"
            result.execution_ms = round((time.perf_counter() - t0) * 1000, 2)
            self._record(fp, validation.tables, True, result)
            return result
        finally:
            timer.cancel()
        result.execution_ms = round((time.perf_counter() - t0) * 1000, 2)

        # 6) 结果检查
        if len(col_names) > MAX_COLUMNS:
            result.query_status = QueryStatus.EXECUTION_ERROR
            result.error = f"返回列数 {len(col_names)} 超过上限 {MAX_COLUMNS}"
            result.error_category = "too_many_columns"
            self._record(fp, validation.tables, True, result)
            return result
        # 双保险：输出列不得包含用户标识
        leaked = [c for c in col_names if c.lower() == "user_id"]
        if leaked:
            result.query_status = QueryStatus.REJECTED
            result.error = "结果包含用户级标识列 user_id，已阻止返回"
            result.error_category = "user_level_leak"
            self._record(fp, validation.tables, True, result,
                         rejection_reason=result.error)
            return result

        result.columns = col_names
        result.row_count = len(fetched)
        if result.row_count > self.max_rows:
            result.rows = fetched[: self.max_rows]
            result.truncated = True
            result.row_count = self.max_rows
            result.warnings.append(
                f"结果超过返回行数上限 {self.max_rows}，已截断（仅返回前 {self.max_rows} 行）")
        else:
            result.rows = fetched
        result.query_status = QueryStatus.OK
        self._record(fp, validation.tables, True, result)
        return result

    # ------------------------------------------------------------------
    def _safe_interrupt(self) -> None:
        try:
            self._con.interrupt()
        except Exception:  # noqa: BLE001 - 连接可能已关闭
            pass

    def _record(self, fingerprint, tables, validation_passed, result,
                rejection_reason=None) -> None:
        self.audit_log.append(AuditRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            fingerprint=fingerprint,
            tables=list(tables),
            validation_passed=validation_passed,
            query_status=result.query_status,
            execution_ms=result.execution_ms,
            row_count=result.row_count,
            truncated=result.truncated,
            rejection_reason=rejection_reason or result.error,
            error_category=result.error_category,
        ))
