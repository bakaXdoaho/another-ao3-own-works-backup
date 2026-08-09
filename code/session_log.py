# session_log.py —— 每次运行自动留一份完整输出
#
# 用法：在脚本最开头加两行
#     import session_log
#     session_log.start("脚本名")
#
# 之后这个脚本打印的一切（包括报错、包括 input() 的提示语）都会同时：
#     · 照常显示在 PyCharm 的运行窗口里
#     · 存进 code/session_printouts/YYYYMMDD-HHMMSS_脚本名.txt
#
# 安全：cookie 的值会被自动打码成 <REDACTED-COOKIE>，即使某个报错不小心把它带出来。
#      （正常情况下没有任何代码会打印 cookie，这是第二道保险。）

from __future__ import annotations

import atexit
import io
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 本模块**只用标准库**，不 import config —— 这样它永远能被最先导入，
# 于是「其它模块 import 时就崩了」这种最该留档的失败也能被记下来。
# （20260804 踩到：config.py 用了 Python 3.10 才有的 `str | None`，
#   在 3.9 上 import 阶段就抛 TypeError，当时整次运行一行日志都没留下。）

CODE_DIR = Path(__file__).resolve().parent
MAIN_DIR = CODE_DIR.parent
PRINTOUT_DIR = CODE_DIR / "session_printouts"
COOKIE_FILE = MAIN_DIR / "secrets" / "ao3_cookie.txt"

_REDACTED = "<REDACTED-COOKIE>"
_started = False
_log_path: Path | None = None


def _secrets_to_scrub() -> list[str]:
    """要打码的字符串。直接读 cookie 文件里出现的每个值，不依赖 config。
    拿不到就算了，绝不能因为打码失败而让脚本挂掉。"""
    out: list[str] = []
    try:
        raw = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    # 粗放地把所有 `name=value` 的 value 都收进来；宁可多打码，不可漏
    import re as _re
    for v in _re.findall(r"=([^;'\"\s]{20,})", raw):
        out.append(v)
        if len(v) > 32:
            out.append(v[:32])          # 万一只被截断打印了一半
    whole = raw.strip()
    if 20 < len(whole) < 4000:
        out.append(whole)
    return sorted(set(out), key=len, reverse=True)   # 长的先替换，免得被短的截断


class _Tee(io.TextIOBase):
    """同时写到终端和文件，中途做打码。"""

    def __init__(self, stream, fh, secrets: list[str]):
        self._stream = stream
        self._fh = fh
        self._secrets = secrets

    def write(self, s: str) -> int:
        clean = s
        for sec in self._secrets:
            if sec and sec in clean:
                clean = clean.replace(sec, _REDACTED)
        self._stream.write(clean)
        self._stream.flush()
        try:
            self._fh.write(clean)
            self._fh.flush()
        except Exception:
            pass                        # 写日志失败绝不影响主流程
        return len(s)

    def flush(self) -> None:
        self._stream.flush()
        try:
            self._fh.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        # input() 会问这个；照实转发给真正的终端
        return getattr(self._stream, "isatty", lambda: False)()


def start(script_name: str) -> Path:
    """开始记录。返回日志文件路径。重复调用无副作用。"""
    global _started, _log_path
    if _started:
        return _log_path                                    # type: ignore[return-value]

    PRINTOUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = PRINTOUT_DIR / f"{stamp}_{script_name}.txt"

    fh = open(path, "w", encoding="utf-8")
    secrets = _secrets_to_scrub()

    fh.write(f"# 运行记录 · {script_name}\n")
    fh.write(f"# 开始时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
    fh.write(f"# Python  ：{sys.version.split()[0]} @ {sys.executable}\n")
    fh.write(f"# 项目目录：{MAIN_DIR}\n")
    fh.write("#" + "-" * 68 + "\n\n")
    fh.flush()

    sys.stdout = _Tee(sys.__stdout__, fh, secrets)
    sys.stderr = _Tee(sys.__stderr__, fh, secrets)

    t0 = time.time()

    def _finish() -> None:
        try:
            # 未捕获的异常在这里补记一笔，免得日志只有半截
            exc = getattr(sys, "last_value", None)
            if exc is not None:
                print("\n[未捕获的异常]")
                traceback.print_exception(type(exc), exc, exc.__traceback__)
            print(f"\n#{'-' * 68}")
            print(f"# 结束时间：{datetime.now():%Y-%m-%d %H:%M:%S}"
                  f"｜耗时 {time.time() - t0:.1f} 秒")
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            fh.close()
        except Exception:
            pass

    atexit.register(_finish)

    _started = True
    _log_path = path
    print(f"（本次运行的完整输出会存到：{path.relative_to(MAIN_DIR)}）\n")
    return path


def crash_dump(script_name: str, exc: BaseException) -> Path | None:
    """给「还没进 main() 就在 import 阶段崩了」的情况留一份最小日志。

    用法（放在每个脚本的模块顶部）：
        import session_log                      # 必须最先，且它只依赖标准库
        try:
            import config
            from ao3_client import ...
        except Exception as e:
            session_log.crash_dump("probe", e)
            raise
    """
    try:
        PRINTOUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = PRINTOUT_DIR / f"{stamp}_{script_name}_CRASH.txt"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# 导入阶段崩溃 · {script_name}\n")
            fh.write(f"# 时间   ：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"# Python ：{sys.version.split()[0]} @ {sys.executable}\n")
            fh.write("#" + "-" * 68 + "\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
        print(f"\n（崩溃详情已存到：{path}）", file=sys.__stderr__)
        return path
    except Exception:
        return None                      # 记日志失败绝不掩盖原始报错
