"""
EchoMind Skill 加载器。

Skill 是一段可热加载的业务能力说明。Agent 先看白名单目录，
再通过 load_skills 工具按需加载正文到当前请求的上下文。
它适合放置企业话术、处理流程、合规边界、排障 SOP 等需要运营侧快速调整的规则。
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """单个 Skill 的标准化表示，屏蔽 Markdown/JSON 等不同文件格式差异。"""
    name: str
    description: str
    content: str
    path: str
    keywords: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    enabled: bool = True

    def matches(self, message: str, agent_type: Optional[str] = None) -> bool:
        """
        判断当前请求是否应该注入这个 Skill。

        - agents 为空：适用于所有 Agent；否则只匹配指定 Agent。
        - keywords 为空：作为全局 Skill 注入；否则只有命中关键词才注入。
        """
        if not self.enabled:
            return False

        if self.agents and agent_type and agent_type.lower() not in self.agents:
            return False

        if not self.keywords:
            return True

        lowered = (message or "").lower()
        return any(keyword.lower() in lowered for keyword in self.keywords)

    def to_prompt_block(self, max_chars: int = 3200) -> str:
        """格式化为可直接拼入 system prompt 的文本块，并限制单个 Skill 长度。"""
        body = self.content.strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n..."
        description = f"\n说明: {self.description}" if self.description else ""
        return f"### {self.name}{description}\n{body}"

    def to_summary(self) -> Dict[str, Any]:
        """返回 API 可序列化摘要，避免把完整长文本默认暴露给健康检查。"""
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "keywords": self.keywords,
            "agents": self.agents,
            "enabled": self.enabled,
            "content_chars": len(self.content),
        }


class SkillManager:
    """
    从目录中发现、解析并管理 Skills。

    支持两种常用结构：
      1. skills/refund/SKILL.md
      2. skills/refund.json / skills/refund.md / skills/refund.txt
    """

    SUPPORTED_SUFFIXES = {".md", ".txt", ".json"}

    def __init__(self, root_dir: str, max_prompt_chars: int = 5000):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.max_prompt_chars = max_prompt_chars
        self._skills: List[Skill] = []
        self._errors: List[str] = []

    @property
    def skills(self) -> List[Skill]:
        return list(self._skills)

    @property
    def errors(self) -> List[str]:
        return list(self._errors)

    def load(self) -> List[Skill]:
        """重新扫描目录并加载 Skills；单个文件失败不会影响其他 Skill 生效。"""
        loaded: List[Skill] = []
        errors: List[str] = []

        if not self.root_dir.exists():
            logger.info(f"Skill 目录不存在，跳过加载: {self.root_dir}")
            self._skills = []
            self._errors = []
            return []

        for path in self._discover_files(self.root_dir):
            try:
                skill = self._load_file(path)
                if skill is not None:
                    loaded.append(skill)
            except Exception as ex:
                msg = f"{path}: {ex}"
                errors.append(msg)
                logger.warning(f"Skill 加载失败: {msg}")

        self._skills = loaded
        self._errors = errors
        self._log_loaded_skills()
        return self.skills

    def reload(self) -> List[Skill]:
        """运行时热加载入口，供 API 调用。"""
        return self.load()

    def available_for(self, agent_type: str) -> List[Skill]:
        """返回某 Agent 真实可访问的 Skill 白名单。"""
        agent = (agent_type or "").strip().lower()
        return [
            skill for skill in self._skills
            if skill.enabled and (not skill.agents or agent in skill.agents)
        ]

    def catalog_for(self, agent_type: str) -> List[Dict[str, Any]]:
        """
        给 Agent 展示轻量目录，不暴露 Skill 正文。

        Agent 根据名称、描述和关键词判断是否需要调用 load_skills。
        """
        return [
            {
                "name": skill.name,
                "description": skill.description,
                "keywords": list(skill.keywords),
            }
            for skill in self.available_for(agent_type)
        ]

    def query_for_agent(
        self,
        query: str,
        agent_type: str,
        *,
        skill_names: Optional[List[str]] = None,
        top_k: int = 3,
    ) -> Dict[str, Any]:
        """
        在 Agent 白名单内按需加载 Skill 正文。

        即使模型显式猜测了其他 Agent 的 Skill 名称，也无法越过
        available_for() 的白名单边界。
        """
        top_k = max(1, min(int(top_k or 3), 5))
        allowed = self.available_for(agent_type)
        allowed_by_name = {skill.name.strip().lower(): skill for skill in allowed}
        requested = [name.strip() for name in (skill_names or []) if name and name.strip()]
        denied: List[str] = []
        ranked: List[tuple[int, Skill, List[str]]] = []
        lowered_query = (query or "").lower()

        if requested:
            for name in requested:
                skill = allowed_by_name.get(name.lower())
                if skill is None:
                    denied.append(name)
                    continue
                matched = [kw for kw in skill.keywords if kw.lower() in lowered_query]
                ranked.append((100, skill, matched))
        else:
            for skill in allowed:
                matched = [kw for kw in skill.keywords if kw.lower() in lowered_query]
                score = len(matched) * 10
                if skill.name.lower() in lowered_query:
                    score += 8
                if skill.description and any(
                    token and token in skill.description.lower()
                    for token in self._query_tokens(lowered_query)
                ):
                    score += 2
                if not skill.keywords:
                    score += 1
                if score > 0:
                    ranked.append((score, skill, matched))

        ranked.sort(key=lambda item: (-item[0], item[1].name))
        remaining = self.max_prompt_chars
        loaded: List[Dict[str, Any]] = []
        truncated = False
        for score, skill, matched in ranked[:top_k]:
            content = skill.content.strip()
            if len(content) > remaining:
                content = content[:remaining].rstrip() + "\n..."
                truncated = True
            loaded.append({
                "name": skill.name,
                "description": skill.description,
                "matched_keywords": matched,
                "instructions": content,
                "score": score,
            })
            remaining -= len(content)
            if remaining <= 0:
                break

        logger.info(
            "Skills 工具加载: agent=%s query=%r loaded=%s denied=%s",
            agent_type,
            (query or "")[:80],
            [item["name"] for item in loaded],
            denied,
        )
        return {
            "agent_type": agent_type,
            "query": query,
            "skills": loaded,
            "loaded_skill_names": [item["name"] for item in loaded],
            "denied_skill_names": denied,
            "truncated": truncated,
        }

    def prompt_for(self, message: str, agent_type: Optional[str] = None) -> str:
        """
        为当前用户请求构建 Skill prompt。

        只注入匹配的 Skill，并按总长度截断，避免挤占主对话上下文。
        """
        blocks: List[str] = []
        matched: List[tuple[Skill, List[str]]] = []
        remaining = self.max_prompt_chars
        lowered_message = (message or "").lower()

        for skill in self._skills:
            if not skill.matches(message, agent_type):
                continue
            matched_keywords = [
                keyword for keyword in skill.keywords
                if keyword.lower() in lowered_message
            ]
            block = skill.to_prompt_block()
            if len(block) > remaining:
                block = block[:remaining].rstrip() + "\n..."
            blocks.append(block)
            matched.append((skill, matched_keywords))
            remaining -= len(block)
            if remaining <= 0:
                break

        if not blocks:
            logger.debug(
                "Skills 未命中: agent=%s message=%r",
                agent_type or "all",
                (message or "")[:80],
            )
            return ""

        detail = "; ".join(
            f"{skill.name}(keywords={', '.join(keywords) if keywords else 'all'})"
            for skill, keywords in matched
        )
        logger.info(
            "Skills 已注入: agent=%s matched=%s message=%r",
            agent_type or "all",
            detail,
            (message or "")[:80],
        )

        return (
            "以下是当前请求可用的 EchoMind Skills。"
            "请优先遵循这些业务规则；如果与系统角色冲突，以系统角色和安全边界为准。\n\n"
            + "\n\n".join(blocks)
        )

    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        """用于轻量描述匹配；主要召回仍由 Skill keywords 负责。"""
        compact = "".join(query.split())
        tokens = [compact] if compact else []
        tokens.extend(compact[index:index + 2] for index in range(max(0, len(compact) - 1)))
        return list(dict.fromkeys(tokens))

    def summary(self) -> Dict[str, Any]:
        """返回 Skill 管理器状态，用于 /skills 接口和排障。"""
        return {
            "root_dir": str(self.root_dir),
            "count": len(self._skills),
            "skills": [skill.to_summary() for skill in self._skills],
            "errors": self.errors,
        }

    def _log_loaded_skills(self) -> None:
        """在控制台输出醒目的 Skill 加载结果，方便启动和热加载时确认生效状态。"""
        lines = [
            "",
            "================ EchoMind Skills Loaded ================",
            f"目录: {self.root_dir}",
            f"数量: {len(self._skills)}",
        ]

        if self._skills:
            for index, skill in enumerate(self._skills, start=1):
                agents = ", ".join(skill.agents) if skill.agents else "all"
                keywords = ", ".join(skill.keywords[:8]) if skill.keywords else "all"
                if len(skill.keywords) > 8:
                    keywords += ", ..."
                lines.extend([
                    f"{index}. {skill.name}",
                    f"   agents: {agents}",
                    f"   keywords: {keywords}",
                    f"   path: {skill.path}",
                ])
        else:
            lines.append("未加载任何 Skill。")

        if self._errors:
            lines.append("解析错误:")
            lines.extend(f"  - {error}" for error in self._errors)

        lines.append("========================================================")
        logger.info("\n".join(lines))

    def _discover_files(self, root_dir: Path) -> Iterable[Path]:
        """发现可加载文件，优先读取目录规范文件 SKILL.md。"""
        skill_md_files = sorted(root_dir.rglob("SKILL.md"))
        yielded = {path.resolve() for path in skill_md_files}
        for path in skill_md_files:
            yield path

        for path in sorted(root_dir.rglob("*")):
            resolved = path.resolve()
            if resolved in yielded or not path.is_file():
                continue
            if path.name.startswith(".") or path.name.upper() == "README.MD":
                continue
            if path.suffix.lower() in self.SUPPORTED_SUFFIXES:
                yield path

    def _load_file(self, path: Path) -> Optional[Skill]:
        if path.suffix.lower() == ".json":
            return self._load_json(path)
        return self._load_text(path)

    def _load_json(self, path: Path) -> Optional[Skill]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("JSON Skill 必须是对象格式")

        content = str(raw.get("content") or raw.get("instructions") or "").strip()
        if not content:
            raise ValueError("缺少 content 或 instructions")

        return Skill(
            name=str(raw.get("name") or path.stem),
            description=str(raw.get("description") or ""),
            content=content,
            path=str(path),
            keywords=self._as_list(raw.get("keywords")),
            agents=[item.lower() for item in self._as_list(raw.get("agents"))],
            enabled=self._as_bool(raw.get("enabled"), default=True),
        )

    def _load_text(self, path: Path) -> Optional[Skill]:
        raw = path.read_text(encoding="utf-8")
        meta, body = self._split_front_matter(raw)
        body = body.strip()
        if not body:
            return None

        default_name = path.parent.name if path.name == "SKILL.md" else path.stem
        name = str(meta.get("name") or self._first_heading(body) or default_name)

        # 如果首行标题只是 Skill 名称，注入 prompt 时去掉它，减少重复噪音。
        body = self._strip_first_heading(body, name)

        return Skill(
            name=name,
            description=str(meta.get("description") or ""),
            content=body,
            path=str(path),
            keywords=self._as_list(meta.get("keywords")),
            agents=[item.lower() for item in self._as_list(meta.get("agents"))],
            enabled=self._as_bool(meta.get("enabled"), default=True),
        )

    def _split_front_matter(self, raw: str) -> tuple[Dict[str, Any], str]:
        """
        解析 Markdown 顶部的简单 front matter。

        这里刻意不用 PyYAML，避免为一个轻量配置格式新增运行时依赖。
        """
        text = raw.lstrip()
        if not text.startswith("---"):
            return {}, raw

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, raw

        meta: Dict[str, Any] = {}
        end_idx: Optional[int] = None
        for idx, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = idx
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")

        if end_idx is None:
            return {}, raw
        return meta, "\n".join(lines[end_idx + 1:])

    @staticmethod
    def _first_heading(body: str) -> Optional[str]:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None

    @staticmethod
    def _strip_first_heading(body: str, name: str) -> str:
        lines = body.splitlines()
        if not lines:
            return body
        first = lines[0].strip()
        if first.startswith("#") and first.lstrip("#").strip() == name:
            return "\n".join(lines[1:]).strip()
        return body

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}
