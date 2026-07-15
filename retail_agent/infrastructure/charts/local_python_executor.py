from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from retail_agent.domain.errors import ChartExecutionError, ChartExecutionFailureCode
from retail_agent.domain.models import ChartArtifact, ChartRequest
from retail_agent.infrastructure.settings import ChartExecutionSettings

_OUTPUT_BASENAME = "chart"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MATPLOTLIB_SVG_DOCTYPE = re.compile(
    br'\s*<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1\.1//EN"\s+'
    br'"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd">\s*'
)
_FORBIDDEN_SVG_ELEMENTS = {
    "animate",
    "animatemotion",
    "animatetransform",
    "embed",
    "foreignobject",
    "iframe",
    "object",
    "script",
    "set",
}
_ALLOWED_IMPORT_ROOTS = {
    "collections",
    "datetime",
    "json",
    "math",
    "matplotlib",
    "numpy",
    "pandas",
    "pathlib",
    "seaborn",
    "statistics",
    "time",
}
_FORBIDDEN_CALL_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "input",
    "locals",
    "open",
    "vars",
}
_FORBIDDEN_ATTRIBUTE_NAMES = {
    "connect",
    "create_connection",
    "environ",
    "execv",
    "execve",
    "fork",
    "getenv",
    "popen",
    "remove",
    "rmtree",
    "run",
    "scandir",
    "spawn",
    "system",
    "unlink",
    "urlopen",
    "walk",
}
_ALLOWED_CHART_PATHS = {"input.json", "chart.png", "chart.svg"}


@dataclass
class _CaptureBudget:
    remaining: int
    exceeded: bool = False


@dataclass
class _CaptureTail:
    limit: int
    content: bytearray = field(default_factory=bytearray)

    def append(self, chunk: bytes) -> None:
        self.content.extend(chunk)
        overflow = len(self.content) - self.limit
        if overflow > 0:
            del self.content[:overflow]


@dataclass(frozen=True)
class _ProcessResult:
    return_code: int
    output_exceeded: bool
    stderr_tail: bytes


class LocalPythonChartExecutor:
    """Execute chart code locally as a bounded reliability boundary.

    This subprocess is not a security sandbox. Production deployments must move
    model-generated code to a separately isolated execution service.
    """

    def __init__(
        self,
        settings: ChartExecutionSettings,
        *,
        temporary_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.temporary_root = temporary_root

    async def execute(self, request: ChartRequest) -> ChartArtifact:
        source = request.code.encode("utf-8")
        if len(source) > self.settings.max_source_bytes:
            raise ChartExecutionError(
                "Chart source exceeds the configured size limit.",
                code="source_too_large",
                repair_hint="Shorten the chart program and keep only plotting logic.",
            )
        _validate_chart_source(request.code)

        code_digest = hashlib.sha256(source).hexdigest()
        temporary_parent = str(self.temporary_root) if self.temporary_root else None
        with tempfile.TemporaryDirectory(
            prefix="retail-chart-",
            dir=temporary_parent,
        ) as directory:
            workdir = Path(directory)
            source_path = workdir / "chart_program.py"
            input_path = workdir / "input.json"
            output_path = workdir / f"{_OUTPUT_BASENAME}.{request.output_format}"
            source_path.write_bytes(source)
            input_path.write_text(
                json.dumps(request.data, default=str, separators=(",", ":")),
                encoding="utf-8",
            )

            result = await self._run_process(source_path, workdir)
            if result.output_exceeded:
                raise ChartExecutionError(
                    "Chart process exceeded the captured output limit.",
                    code="captured_output_limit",
                    repair_hint="Remove print calls and generate only the requested chart file.",
                )
            if result.return_code != 0:
                error_code, repair_hint = _diagnose_process_failure(
                    result.stderr_tail,
                    request,
                )
                raise ChartExecutionError(
                    "Chart process failed before producing a valid artifact.",
                    code=error_code,
                    repair_hint=repair_hint,
                )

            size_bytes = self._validate_output(output_path, request.output_format)
            destination = self._publish_artifact(
                output_path,
                request.output_format,
                code_digest,
            )
            return ChartArtifact(
                path=str(destination),
                output_format=request.output_format,
                size_bytes=size_bytes,
                code_digest=code_digest,
            )

    async def _run_process(self, source_path: Path, workdir: Path) -> _ProcessResult:
        environment = {
            "HOME": str(workdir),
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(workdir / ".matplotlib"),
            "PATH": os.defpath,
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(workdir),
            "TZ": "UTC",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                str(source_path),
                cwd=workdir,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ChartExecutionError(
                "Chart process could not be started.",
                code="process_failed",
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        budget = _CaptureBudget(self.settings.max_captured_output_bytes)
        stderr_tail = _CaptureTail(min(self.settings.max_captured_output_bytes, 8_192))
        stdout_task = asyncio.create_task(_drain_bounded(process.stdout, budget))
        stderr_task = asyncio.create_task(
            _drain_bounded(process.stderr, budget, capture=stderr_tail)
        )
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.settings.timeout_seconds,
            )
        except asyncio.CancelledError:
            _kill_process_group(process)
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        except TimeoutError as exc:
            _kill_process_group(process)
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            raise ChartExecutionError(
                "Chart process exceeded the configured timeout.",
                code="timeout",
                repair_hint="Simplify the chart program and avoid unbounded loops.",
            ) from exc
        finally:
            _kill_process_group(process)

        await asyncio.gather(stdout_task, stderr_task)
        return _ProcessResult(
            return_code=process.returncode or 0,
            output_exceeded=budget.exceeded,
            stderr_tail=bytes(stderr_tail.content),
        )

    def _validate_output(self, output_path: Path, output_format: str) -> int:
        if output_path.is_symlink() or not output_path.is_file():
            raise ChartExecutionError(
                "Chart process did not create the expected output file.",
                code="output_missing",
                repair_hint=(
                    f'Save exactly one file named "{_OUTPUT_BASENAME}.{output_format}" '
                    "in the current working directory."
                ),
            )
        size_bytes = output_path.stat().st_size
        if size_bytes <= 0:
            raise ChartExecutionError(
                "Chart output is empty.",
                code="invalid_output",
            )
        if size_bytes > self.settings.max_output_bytes:
            raise ChartExecutionError(
                "Chart output exceeds the configured size limit.",
                code="output_too_large",
            )

        content = output_path.read_bytes()
        if output_format == "png":
            if not content.startswith(_PNG_SIGNATURE):
                raise ChartExecutionError(
                    "Chart output is not a valid PNG file.",
                    code="invalid_output",
                )
        elif output_format == "svg":
            sanitized = _validate_svg(content)
            if sanitized != content:
                output_path.write_bytes(sanitized)
                size_bytes = len(sanitized)
        else:  # ChartRequest validation should make this unreachable.
            raise ChartExecutionError(
                "Chart output format is unsupported.",
                code="invalid_output",
            )
        return size_bytes

    def _publish_artifact(
        self,
        source: Path,
        output_format: str,
        code_digest: str,
    ) -> Path:
        configured_directory = self.settings.artifact_directory
        artifact_directory = configured_directory.resolve()
        artifact_directory.mkdir(parents=True, exist_ok=True)
        filename = f"chart-{code_digest[:16]}-{uuid.uuid4().hex[:12]}.{output_format}"
        destination = artifact_directory / filename
        temporary_destination = artifact_directory / f".{filename}.tmp"
        try:
            shutil.copyfile(source, temporary_destination)
            os.replace(temporary_destination, destination)
        finally:
            temporary_destination.unlink(missing_ok=True)
        if configured_directory.is_absolute():
            return destination
        return configured_directory / filename


async def _drain_bounded(
    stream: asyncio.StreamReader,
    budget: _CaptureBudget,
    *,
    capture: _CaptureTail | None = None,
) -> None:
    while chunk := await stream.read(64 * 1024):
        if capture is not None:
            capture.append(chunk)
        if len(chunk) > budget.remaining:
            budget.exceeded = True
            budget.remaining = 0
        else:
            budget.remaining -= len(chunk)


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    if process.returncode is None:  # pragma: no cover - Windows fallback
        process.kill()


def _validate_svg(content: bytes) -> bytes:
    sanitized = _MATPLOTLIB_SVG_DOCTYPE.sub(b"\n", content, count=1)
    if b"<!DOCTYPE" in sanitized.upper():
        raise ChartExecutionError(
            "Chart SVG contains a disallowed document type declaration.",
            code="invalid_output",
        )
    try:
        root = ET.fromstring(sanitized)
    except ET.ParseError as exc:
        raise ChartExecutionError(
            "Chart output is not valid SVG XML.",
            code="invalid_output",
        ) from exc
    if _local_name(root.tag) != "svg":
        raise ChartExecutionError(
            "Chart output root element is not SVG.",
            code="invalid_output",
        )
    for element in root.iter():
        element_name = _local_name(element.tag)
        if element_name in _FORBIDDEN_SVG_ELEMENTS:
            raise ChartExecutionError(
                "Chart SVG contains disallowed active content.",
                code="invalid_output",
            )
        if element_name == "style":
            style_text = "".join(element.itertext()).strip().casefold()
            if "@import" in style_text or (
                "url(" in style_text and "url(#" not in style_text
            ):
                raise ChartExecutionError(
                    "Chart SVG contains a disallowed external style resource.",
                    code="invalid_output",
                )
        for name, value in element.attrib.items():
            attribute = _local_name(name)
            normalized_value = value.strip().lower()
            if attribute.startswith("on") or (
                attribute == "href" and normalized_value and not normalized_value.startswith("#")
            ):
                raise ChartExecutionError(
                    "Chart SVG contains disallowed external or active content.",
                    code="invalid_output",
                )
            if "url(" in normalized_value and "url(#" not in normalized_value:
                raise ChartExecutionError(
                    "Chart SVG contains a disallowed external resource.",
                    code="invalid_output",
                )
    return sanitized


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].casefold()


def _validate_chart_source(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ChartExecutionError(
            "Chart source is not valid Python.",
            code="syntax_error",
            repair_hint=f"Fix the Python syntax near line {exc.lineno or 1}.",
        ) from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
            _ensure_allowed_imports(imported)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                _reject_unsafe_source()
            _ensure_allowed_imports([node.module])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALL_NAMES:
                _reject_unsafe_source()
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "Path"
                and not _is_allowed_path_call(node)
            ):
                _reject_unsafe_source()
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in _FORBIDDEN_ATTRIBUTE_NAMES:
                _reject_unsafe_source()


def _ensure_allowed_imports(modules: list[str]) -> None:
    if any(module.split(".", 1)[0] not in _ALLOWED_IMPORT_ROOTS for module in modules):
        _reject_unsafe_source()


def _is_allowed_path_call(node: ast.Call) -> bool:
    return (
        len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value in _ALLOWED_CHART_PATHS
    )


def _reject_unsafe_source() -> None:
    raise ChartExecutionError(
        "Chart source requests file, network, process, environment, or dynamic-code access.",
        code="unsafe_source",
    )


def _diagnose_process_failure(
    stderr_tail: bytes,
    request: ChartRequest,
) -> tuple[ChartExecutionFailureCode, str]:
    stderr = stderr_tail.decode("utf-8", errors="replace").strip()
    if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
        error_code: ChartExecutionFailureCode = "missing_dependency"
    elif "SyntaxError" in stderr or "IndentationError" in stderr:
        error_code = "syntax_error"
    elif any(name in stderr for name in ("KeyError", "TypeError", "ValueError", "IndexError")):
        error_code = "data_shape_error"
    else:
        error_code = "runtime_error"

    line_match = re.search(r'File "[^"]*chart_program\.py", line (\d+)', stderr)
    line_hint = f" at line {line_match.group(1)}" if line_match else ""
    detail = _last_exception_line(stderr)
    columns = sorted({str(key) for row in request.data for key in row})
    column_hint = ", ".join(columns[:30]) if columns else "(no columns; the result was empty)"
    expected_output = f"chart.{request.output_format}"
    return (
        error_code,
        f"Chart execution failed with {error_code}{line_hint}. {detail} "
        f"Available columns: {column_hint}. Read input.json and save {expected_output}.",
    )


def _last_exception_line(stderr: str) -> str:
    for line in reversed(stderr.splitlines()):
        normalized = line.strip()
        if not normalized or normalized.startswith(("Traceback ", "File ")):
            continue
        normalized = re.sub(r'"[^"\n]*chart_program\.py"', '"chart_program.py"', normalized)
        return normalized[:300]
    return "The chart program exited without a Python error message."
