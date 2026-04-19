from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .cli_adapters import get_adapter
    from .config_loader import AgentConfig, ProjectConfig, find_agent
except ImportError:  # pragma: no cover - script mode support
    from cli_adapters import get_adapter
    from config_loader import AgentConfig, ProjectConfig, find_agent

LOGGER = logging.getLogger(__name__)
DEFAULT_FINDER_PROMPT = """You are helping resume a local software project from Discord.

Choose the best local folder candidate for the operator's query.
Return JSON only with this exact shape:
{
  "status": "selected" | "needs_clarification" | "no_match",
  "selected_path": "absolute path or null",
  "selected_name": "human-friendly project name or null",
  "reason": "short explanation",
  "confidence": 0.0,
  "candidates": [
    {
      "path": "absolute path",
      "display_name": "short project name",
      "rationale": "why it is relevant",
      "score": 0.0
    }
  ]
}

Rules:
- Only choose a path from the provided candidate list.
- Use "needs_clarification" if two or more candidates are similarly plausible.
- Use "no_match" if none of the candidates are convincing.
- Keep reasons short and concrete.
"""

MARKER_NAMES = [
    "project.godot",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "README.md",
    ".git",
    "src",
    "scripts",
    "scenes",
    "Assets",
]
MAX_WALKED_DIRECTORIES = 2500


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def tokenize(value: str) -> list[str]:
    return [token for token in normalize(value).split() if token]


@dataclass(slots=True)
class CandidateSummary:
    path: Path
    display_name: str
    heuristic_score: float
    rationale: str
    markers: list[str]
    modified_at: str

    def to_agent_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "display_name": self.display_name,
            "heuristic_score": round(self.heuristic_score, 3),
            "rationale": self.rationale,
            "markers": self.markers,
            "modified_at": self.modified_at,
        }

    def to_bridge_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "display_name": self.display_name,
            "rationale": self.rationale,
            "score": round(self.heuristic_score, 3),
        }


class ProjectFinder:
    def __init__(self, *, project_file: Path, project: ProjectConfig) -> None:
        self.project_file = Path(project_file).resolve()
        self.project = project
        self.finder = project.finder
        self.agent = self._select_agent()
        self.adapter = get_adapter(self.agent.cli)
        self.prompt_text = self._load_prompt_text()

    def find(self, query_text: str) -> dict[str, object]:
        candidates = self._discover_candidates(query_text)
        if not candidates:
            return {
                "status": "no_match",
                "selected_path": None,
                "selected_name": None,
                "reason": "No plausible project folders were found under the configured roots.",
                "confidence": 0.0,
                "candidates": [],
            }

        try:
            decision = self._run_agent_analysis(query_text=query_text, candidates=candidates)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Project finder agent analysis failed: %s", exc)
            decision = self._fallback_decision(candidates)

        return self._normalize_decision(decision=decision, candidates=candidates)

    def _discover_candidates(self, query_text: str) -> list[CandidateSummary]:
        query_tokens = tokenize(query_text)
        roots = [Path(root).resolve() for root in self.finder.roots]
        exclude = {name.lower() for name in self.finder.exclude_dirs}
        candidates: list[CandidateSummary] = []
        walked = 0

        for root in roots:
            if not root.exists() or not root.is_dir():
                continue

            for directory in self._walk_directories(root=root, max_depth=self.finder.max_depth, exclude=exclude):
                walked += 1
                if walked > MAX_WALKED_DIRECTORIES:
                    break
                candidate = self._build_candidate(directory=directory, query_tokens=query_tokens)
                if candidate is None:
                    continue
                candidates.append(candidate)
            if walked > MAX_WALKED_DIRECTORIES:
                break

        candidates.sort(key=lambda item: (-item.heuristic_score, item.display_name.lower(), str(item.path).lower()))
        deduped: list[CandidateSummary] = []
        seen_paths: set[str] = set()
        for candidate in candidates:
            raw_path = str(candidate.path)
            if raw_path in seen_paths:
                continue
            seen_paths.add(raw_path)
            deduped.append(candidate)
            if len(deduped) >= max(3, self.finder.max_candidates):
                break
        return deduped

    def _walk_directories(self, *, root: Path, max_depth: int, exclude: set[str]) -> list[Path]:
        results: list[Path] = []
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            results.append(current)
            if depth >= max_depth:
                continue
            try:
                children = sorted(
                    [child for child in current.iterdir() if child.is_dir()],
                    key=lambda path: path.name.lower(),
                    reverse=True,
                )
            except OSError:
                continue
            for child in children:
                lowered = child.name.lower()
                if lowered in exclude:
                    continue
                if lowered.startswith(".") and lowered not in {".config", ".github"}:
                    continue
                stack.append((child, depth + 1))
        return results

    def _build_candidate(self, *, directory: Path, query_tokens: list[str]) -> CandidateSummary | None:
        markers = self._detect_markers(directory)
        rel_tokens = tokenize(directory.name)
        full_text = normalize(str(directory))
        score = 0.0
        matched_tokens: list[str] = []
        for token in query_tokens:
            if token in rel_tokens:
                score += 3.0
                matched_tokens.append(token)
            elif token in full_text:
                score += 1.2
                matched_tokens.append(token)
        normalized_name = normalize(directory.name)
        normalized_query = " ".join(query_tokens)
        if normalized_query and normalized_query in normalized_name:
            score += 3.5
        if normalized_query and normalized_query and normalized_query in full_text:
            score += 2.0
        if markers:
            score += min(2.5, len(markers) * 0.35)

        if score <= 0 and not markers:
            return None

        display_name = self._derive_display_name(directory)
        rationale_parts = []
        if matched_tokens:
            rationale_parts.append(f"query tokens matched: {', '.join(sorted(set(matched_tokens)))}")
        if markers:
            rationale_parts.append(f"markers: {', '.join(markers[:4])}")
        if not rationale_parts:
            rationale_parts.append("candidate found by directory scan")

        try:
            modified_at = datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            modified_at = utcnow().isoformat()

        return CandidateSummary(
            path=directory,
            display_name=display_name,
            heuristic_score=score,
            rationale="; ".join(rationale_parts),
            markers=markers,
            modified_at=modified_at,
        )

    @staticmethod
    def _detect_markers(directory: Path) -> list[str]:
        markers: list[str] = []
        for marker in MARKER_NAMES:
            try:
                if (directory / marker).exists():
                    markers.append(marker)
            except OSError:
                continue
        return markers

    def _derive_display_name(self, directory: Path) -> str:
        project_godot = directory / "project.godot"
        if project_godot.exists():
            try:
                text = project_godot.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'^config/name="(?P<name>.+?)"$', text, re.MULTILINE)
                if match:
                    return match.group("name").strip()
            except OSError:
                pass

        package_json = directory / "package.json"
        if package_json.exists():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
                name = payload.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
            except Exception:  # noqa: BLE001
                pass

        return directory.name

    def _run_agent_analysis(self, *, query_text: str, candidates: list[CandidateSummary]) -> dict[str, object]:
        command = self.adapter.build_command()
        env = self._build_subprocess_env()
        cwd = Path(self.finder.roots[0]).resolve() if self.finder.roots else Path(self.project.workdir).resolve()
        payload = {
            "query": query_text,
            "preset": self.project.project_name,
            "allowed_roots": self.finder.roots,
            "candidates": [candidate.to_agent_payload() for candidate in candidates],
        }
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        try:
            stdout, stderr = process.communicate(
                input=self._build_agent_input(payload),
                timeout=max(30, self.finder.analysis_timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            raise TimeoutError("Project finder analysis timed out.") from exc

        combined = self.adapter.combine_output(stdout, stderr, process.returncode or 0)
        if process.returncode not in (0, None):
            raise RuntimeError(combined)
        return self._parse_json_output(combined)

    def _build_agent_input(self, payload: dict[str, object]) -> str:
        return (
            f"System prompt:\n{self.prompt_text}\n\n"
            "Search payload JSON:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Return JSON only.\n"
        )

    def _normalize_decision(
        self,
        *,
        decision: dict[str, object],
        candidates: list[CandidateSummary],
    ) -> dict[str, object]:
        allowed_paths = {str(candidate.path): candidate for candidate in candidates}
        status = str(decision.get("status") or "needs_clarification").strip().lower()
        if status not in {"selected", "needs_clarification", "no_match"}:
            status = "needs_clarification"

        selected_path = decision.get("selected_path")
        if isinstance(selected_path, str):
            selected_path = str(Path(selected_path).resolve())
        else:
            selected_path = None

        if status == "selected" and selected_path not in allowed_paths:
            status = "needs_clarification"
            selected_path = None

        selected_candidate = allowed_paths.get(selected_path) if selected_path else None
        reason = decision.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "The finder could not explain the choice clearly."

        raw_candidates = decision.get("candidates")
        normalized_candidates: list[dict[str, object]] = []
        if isinstance(raw_candidates, list):
            for item in raw_candidates[: self.finder.max_candidates]:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                if not isinstance(path, str):
                    continue
                resolved_path = str(Path(path).resolve())
                candidate = allowed_paths.get(resolved_path)
                if candidate is None:
                    continue
                rationale = item.get("rationale")
                normalized_candidates.append(
                    {
                        "path": resolved_path,
                        "display_name": candidate.display_name,
                        "rationale": rationale if isinstance(rationale, str) and rationale.strip() else candidate.rationale,
                        "score": float(item.get("score")) if isinstance(item.get("score"), (int, float)) else round(candidate.heuristic_score, 3),
                    },
                )

        if not normalized_candidates:
            normalized_candidates = [candidate.to_bridge_payload() for candidate in candidates[: min(3, len(candidates))]]

        confidence = decision.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence_value = max(0.0, min(1.0, float(confidence)))
        else:
            confidence_value = 0.0

        if status != "selected" and candidates:
            top = candidates[0]
            second_score = candidates[1].heuristic_score if len(candidates) > 1 else 0.0
            if top.heuristic_score >= 5.0 and top.heuristic_score - second_score >= 2.0:
                status = "selected"
                selected_path = str(top.path)
                selected_candidate = top
                confidence_value = max(confidence_value, min(0.9, 0.5 + (top.heuristic_score / 12.0)))
                if not reason:
                    reason = f"Top project-root candidate dominated the local scan: {top.rationale}"

        return {
            "status": status,
            "selected_path": selected_path,
            "selected_name": selected_candidate.display_name if selected_candidate is not None else decision.get("selected_name"),
            "reason": reason.strip(),
            "confidence": confidence_value,
            "candidates": normalized_candidates,
        }

    @staticmethod
    def _parse_json_output(text: str) -> dict[str, object]:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            return json.loads(stripped[start : end + 1])

    def _fallback_decision(self, candidates: list[CandidateSummary]) -> dict[str, object]:
        top = candidates[0]
        second_score = candidates[1].heuristic_score if len(candidates) > 1 else 0.0
        if top.heuristic_score >= 4.0 and top.heuristic_score - second_score >= 1.5:
            return {
                "status": "selected",
                "selected_path": str(top.path),
                "selected_name": top.display_name,
                "reason": f"Best heuristic match: {top.rationale}",
                "confidence": min(0.85, 0.45 + (top.heuristic_score / 10.0)),
                "candidates": [candidate.to_bridge_payload() for candidate in candidates[:3]],
            }
        return {
            "status": "needs_clarification",
            "selected_path": None,
            "selected_name": None,
            "reason": "Several local folders look similar, so the result needs clarification.",
            "confidence": 0.25,
            "candidates": [candidate.to_bridge_payload() for candidate in candidates[:3]],
        }

    def _load_prompt_text(self) -> str:
        if self.finder.prompt_file:
            prompt_path = (self.project_file.parent / self.finder.prompt_file).resolve()
            return prompt_path.read_text(encoding="utf-8")
        return DEFAULT_FINDER_PROMPT

    def _select_agent(self) -> AgentConfig:
        preferred = self.finder.analyze_agent or "planner"
        try:
            return find_agent(self.project, preferred)
        except ValueError:
            pass
        defaults = [agent for agent in self.project.agents if agent.default]
        if defaults:
            return defaults[0]
        return self.project.agents[0]

    @staticmethod
    def _build_subprocess_env() -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["LANG"] = env.get("LANG") or "C.UTF-8"
        env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
        env["NO_COLOR"] = "1"
        return env
