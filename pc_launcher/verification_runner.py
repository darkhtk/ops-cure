from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    from .config_loader import ProjectConfig
    from .process_io import build_utf8_subprocess_env, decode_text_output, text_subprocess_kwargs, wrap_powershell_utf8
except ImportError:  # pragma: no cover - script mode support
    from config_loader import ProjectConfig
    from process_io import build_utf8_subprocess_env, decode_text_output, text_subprocess_kwargs, wrap_powershell_utf8

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".gif"}
REPORT_EXTENSIONS = {".json", ".md", ".txt", ".log"}


@dataclass(slots=True)
class VerificationResult:
    status: str
    summary_text: str | None
    error_text: str | None
    artifacts: list[dict[str, object]]


class CommandVerificationRunner:
    def run(
        self,
        *,
        run_payload: dict[str, object],
        project: ProjectConfig,
    ) -> VerificationResult:
        workdir = Path(str(run_payload["workdir"])).resolve()
        artifact_dir = Path(str(run_payload["artifact_dir"])).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        command = [str(part) for part in run_payload["command"]]
        timeout_seconds = int(run_payload["timeout_seconds"])
        env = build_utf8_subprocess_env(
            extra={
                "OPS_CURE_VERIFY_DIR": str(artifact_dir),
                "OPS_CURE_VERIFY_RUN_ID": str(run_payload["id"]),
                "OPS_CURE_VERIFY_SESSION_ID": str(run_payload["session_id"]),
                "OPS_CURE_VERIFY_MODE": str(run_payload["mode"]),
                "OPS_CURE_VERIFY_PROFILE": project.profile_name,
            },
        )

        stdout_path = artifact_dir / "stdout.log"
        stderr_path = artifact_dir / "stderr.log"
        stdout_bin_path = artifact_dir / "stdout.bin"
        stderr_bin_path = artifact_dir / "stderr.bin"
        result_path = artifact_dir / "result.json"

        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                timeout=timeout_seconds,
                env=env,
                **text_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = decode_text_output(exc.stdout)
            stderr_text = decode_text_output(exc.stderr)
            stdout_path.write_text(stdout_text, encoding="utf-8")
            stderr_path.write_text(stderr_text, encoding="utf-8")
            stdout_bin_path.write_bytes((stdout_text or "").encode("utf-8", errors="replace"))
            stderr_bin_path.write_bytes((stderr_text or "").encode("utf-8", errors="replace"))
            if project.verification.capture.screenshots:
                self._capture_desktop_screenshot(artifact_dir)
            summary = (
                f"Verification `{run_payload['mode']}` timed out after {timeout_seconds}s "
                f"for profile `{project.profile_name}`."
            )
            result_path.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "summary": summary,
                        "error": summary,
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            artifacts = self._collect_artifacts(artifact_dir)
            return VerificationResult(
                status="failed",
                summary_text=None,
                error_text=summary,
                artifacts=artifacts,
            )

        stdout_text = decode_text_output(completed.stdout)
        stderr_text = decode_text_output(completed.stderr)
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        stdout_bin_path.write_bytes(stdout_text.encode("utf-8", errors="replace"))
        stderr_bin_path.write_bytes(stderr_text.encode("utf-8", errors="replace"))
        if project.verification.capture.screenshots:
            self._capture_desktop_screenshot(artifact_dir)

        if completed.returncode == 0:
            status = "completed"
            summary_text = self._build_success_summary(
                mode=str(run_payload["mode"]),
                profile_name=project.profile_name,
                artifact_dir=artifact_dir,
            )
            error_text = None
        else:
            status = "failed"
            summary_text = None
            error_text = (
                f"Verification `{run_payload['mode']}` exited with code `{completed.returncode}` "
                f"for profile `{project.profile_name}`."
            )

        result_path.write_text(
            json.dumps(
                {
                    "status": status,
                    "summary": summary_text,
                    "error": error_text,
                    "returncode": completed.returncode,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        return VerificationResult(
            status=status,
            summary_text=summary_text,
            error_text=error_text,
            artifacts=self._collect_artifacts(artifact_dir),
        )

    def _collect_artifacts(self, artifact_dir: Path) -> list[dict[str, object]]:
        artifacts: list[dict[str, object]] = []
        for path in sorted(artifact_dir.rglob("*")):
            if not path.is_file():
                continue
            artifacts.append(
                {
                    "artifact_type": self._classify_artifact(path),
                    "label": path.name,
                    "path": str(path),
                },
            )
        return artifacts

    @staticmethod
    def _classify_artifact(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return "screenshot"
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in REPORT_EXTENSIONS:
            return "report"
        return "file"

    def _build_success_summary(self, *, mode: str, profile_name: str, artifact_dir: Path) -> str:
        screenshots = [
            path.name
            for path in sorted(artifact_dir.rglob("*"))
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if screenshots:
            return (
                f"Verification `{mode}` completed for profile `{profile_name}`. "
                f"Representative screenshot: `{screenshots[0]}`."
            )
        return (
            f"Verification `{mode}` completed for profile `{profile_name}`. "
            f"Artifacts were written to `{artifact_dir}`."
        )

    @staticmethod
    def _capture_desktop_screenshot(artifact_dir: Path) -> None:
        if os.name != "nt":  # pragma: no cover - only used on Windows
            return
        screenshot_path = artifact_dir / "desktop.png"
        escaped_path = str(screenshot_path).replace("'", "''")
        powershell = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$bounds=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
            "$bitmap=New-Object System.Drawing.Bitmap $bounds.Width,$bounds.Height; "
            "$graphics=[System.Drawing.Graphics]::FromImage($bitmap); "
            "$graphics.CopyFromScreen($bounds.Location,[System.Drawing.Point]::Empty,$bounds.Size); "
            f"$bitmap.Save('{escaped_path}',[System.Drawing.Imaging.ImageFormat]::Png); "
            "$graphics.Dispose(); "
            "$bitmap.Dispose();"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", wrap_powershell_utf8(powershell)],
                capture_output=True,
                timeout=30,
                check=False,
                **text_subprocess_kwargs(),
            )
        except Exception:
            return
