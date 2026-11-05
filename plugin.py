"""CAYRememberYou —— 跨群记忆插件（MaiBot 1.3+ / maibot_sdk v2）

做什么：
    把「别的群聊过什么」变成麦麦的记忆。三个入口：
    1. 钩子 chat.receive.before_process：每条入站消息，按内容检索其他群的相关片段，
       以文本段形式追加到消息里（模型直接能看到）；
    2. 工具 search_cross_group_memory：模型可以主动去查；
    3. 命令 /跨群记忆 <关键词> 与 /跨群记忆 统计。

数据来源：
    直接只读 MaiBot 自己的 SQLite（mai_messages / 旧版 messages），
    不新建表、不写库、不依赖 ORM 类，所以跨版本不会因为模型改名而崩。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import field_validator

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

PLUGIN_VERSION = "3.8.0"


# ---------------------------------------------------------------- 配置模型 ----


class PluginSection(PluginConfigBase):
    """插件基础配置 / Plugin basics."""

    __ui_label__ = "插件 / Plugin"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用跨群记忆 / Enable cross-group memory",
        json_schema_extra={"label": "启用插件 / Enabled"},
    )
    config_version: str = Field(
        default=PLUGIN_VERSION,
        description="配置版本号，改结构时递增 / Config schema version",
        json_schema_extra={"label": "配置版本 / Config version"},
    )


class MemorySection(PluginConfigBase):
    """检索与注入参数 / Retrieval and injection."""

    __ui_label__ = "记忆检索 / Memory retrieval"
    __ui_order__ = 10

    inject_enabled: bool = Field(
        default=True,
        description="每条入站消息自动检索其他群的相关片段 / Auto-inject related snippets from other chats",
        json_schema_extra={"label": "自动注入 / Auto inject"},
    )
    decay_mode: Literal["记忆曲线 / Curve", "固定窗口 / Window"] = Field(
        default="记忆曲线 / Curve",
        description="window=固定时间窗，超过即忘；curve=拟人记忆曲线，越久远越模糊 / "
        "window=fixed cutoff; curve=human-like forgetting curve",
        json_schema_extra={"label": "回溯方式 / Recall mode"},
    )

    @field_validator("decay_mode", mode="before")
    @classmethod
    def _coerce_decay_mode(cls, value: Any) -> Any:
        """兼容旧配置里的 window / curve。"""
        if isinstance(value, str):
            return "固定窗口 / Window" if _norm_decay(value) == "window" else "记忆曲线 / Curve"
        return value
    lookback_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="固定窗口=只看最近多少天；记忆曲线=最长回溯上限 / "
        "window mode: days to look back; curve mode: hard upper bound",
        json_schema_extra={"label": "回溯天数 / Lookback days"},
    )
    half_life_days: float = Field(
        default=7.0,
        ge=0.5,
        le=365.0,
        description="记忆曲线半衰期：每过这么多天权重减半 / Half-life: weight halves every N days",
        json_schema_extra={"label": "记忆半衰期(天) / Half-life (days)"},
    )
    min_decay_weight: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="记忆曲线遗忘下限，低于该值视为想不起来 / Forget floor: below this weight a memory is dropped",
        json_schema_extra={"label": "遗忘下限 / Forget floor"},
    )
    scan_limit: int = Field(
        default=2000,
        ge=100,
        le=5000,
        description="每次扫描最近多少条消息，越大越慢 / Recent messages scanned per query",
        json_schema_extra={"label": "扫描条数 / Scan limit"},
    )
    max_snippets: int = Field(
        default=4,
        ge=1,
        le=10,
        description="最多注入几条记忆 / Max snippets injected",
        json_schema_extra={"label": "最多条数 / Max snippets"},
    )
    min_score: float = Field(
        default=0.18,
        ge=0.0,
        le=1.0,
        description="相关度阈值，越高越严格 / Relevance threshold, higher is stricter",
        json_schema_extra={"label": "相关度阈值 / Min score"},
    )
    min_message_chars: int = Field(
        default=6,
        ge=2,
        le=100,
        description="忽略短于该长度的消息 / Ignore messages shorter than this",
        json_schema_extra={"label": "最短消息长度 / Min message length"},
    )
    max_chars_per_message: int = Field(
        default=120,
        ge=20,
        le=400,
        description="单条记忆的截断长度 / Truncate each snippet to this length",
        json_schema_extra={"label": "单条截断长度 / Max chars per snippet"},
    )
    skip_bot_messages: bool = Field(
        default=True,
        description="忽略机器人自己的发言 / Ignore the bot's own messages",
        json_schema_extra={"label": "跳过机器人发言 / Skip bot messages"},
    )
    skip_commands: bool = Field(
        default=True,
        description="忽略命令消息 / Ignore command messages",
        json_schema_extra={"label": "跳过命令消息 / Skip commands"},
    )


class PrivacySection(PluginConfigBase):
    """隐私保护 / Privacy."""

    __ui_label__ = "隐私 / Privacy"
    __ui_order__ = 20

    private_as_source: bool = Field(
        default=False,
        description="私聊内容是否可被检索并外传 / Allow private chats to be used as memory",
        json_schema_extra={"label": "私聊内容可被检索 / Private as source"},
    )
    private_as_target: bool = Field(
        default=True,
        description="私聊里是否显示跨群记忆 / Show cross-group memory inside private chats",
        json_schema_extra={"label": "私聊可接收记忆 / Private as target"},
    )
    mask_nicknames: bool = Field(
        default=False,
        description="注入时隐去发言人昵称 / Hide sender nicknames in snippets",
        json_schema_extra={"label": "隐藏昵称 / Mask nicknames"},
    )
    blocked_stream_ids: list[str] = Field(
        default_factory=list,
        description="完全不参与的会话 ID，一行一个 / Chat ids excluded entirely",
        json_schema_extra={"label": "排除的会话 / Blocked chats"},
    )


class GroupsSection(PluginConfigBase):
    """群组访问控制 / Group access control."""

    __ui_label__ = "群组范围 / Groups"
    __ui_order__ = 25

    mode: Literal["全部群 / All", "全部禁止 / None", "仅白名单 / Whitelist", "除黑名单外 / Blacklist"] = Field(
        default="全部群 / All",
        description="all=全部群参与；none=全禁；whitelist=仅名单；blacklist=除名单外 / "
        "all, none, whitelist, blacklist",
        json_schema_extra={"label": "群组模式 / Group mode"},
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, value: Any) -> Any:
        """兼容旧配置里的 all / none / whitelist / blacklist。"""
        if isinstance(value, str):
            norm = _norm_group_mode(value)
            return {
                "none": "全部禁止 / None",
                "whitelist": "仅白名单 / Whitelist",
                "blacklist": "除黑名单外 / Blacklist",
            }.get(norm, "全部群 / All")
        return value
    group_ids: list[str] = Field(
        default_factory=list,
        description="名单：群号或群名，一行一个 / Group ids or names, one per line",
        json_schema_extra={"label": "群号/群名名单 / Group list"},
    )


class CommandSection(PluginConfigBase):
    """命令开关 / Commands."""

    __ui_label__ = "命令 / Commands"
    __ui_order__ = 30

    enabled: bool = Field(
        default=True,
        description="启用 /跨群记忆 命令 / Enable the /跨群记忆 command",
        json_schema_extra={"label": "启用命令 / Enable command"},
    )


class SemanticSection(PluginConfigBase):
    """向量语义检索（可选）。"""

    __ui_label__ = "语义向量 / Semantic"
    __ui_order__ = 50

    enabled: bool = Field(
        default=False,
        description=(
            "是否连接记忆向量库做语义检索。⚠ 开启后每次检索会多一次 embedding 请求，"
            "Token 与额度消耗随之增长（首次建缓存时更明显）；关闭则只用关键词匹配，零额外开销。"
        ),
        json_schema_extra={"label": "启用向量语义（会增加 Token 消耗）"},
    )
    rerank_limit: int = Field(
        default=20, ge=4, le=100,
        description="只对关键词召回的 Top N 条做向量重排，控制成本",
        json_schema_extra={"label": "重排条数（越小越省）"},
    )
    min_similarity: float = Field(
        default=0.35, ge=0.0, le=1.0,
        description="余弦相似度阈值，低于它不采用",
        json_schema_extra={"label": "相似度阈值"},
    )


class CAYConfig(PluginConfigBase):
    """完整配置 / Full config."""

    plugin: PluginSection = Field(default_factory=PluginSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    privacy: PrivacySection = Field(default_factory=PrivacySection)
    groups: GroupsSection = Field(default_factory=GroupsSection)
    command: CommandSection = Field(default_factory=CommandSection)
    semantic: SemanticSection = Field(default_factory=SemanticSection)


# `from __future__ import annotations` 会把注解变成字符串，pydantic 需要在
# 所有模型类都定义完之后重建一次，否则会报 "not fully defined"。
for _model in (PluginSection, MemorySection, PrivacySection, GroupsSection, CommandSection, SemanticSection, CAYConfig):
    _model.model_rebuild()

# ---------------------------------------------------------------- 文本工具 ----

_SEGMENT_RE = re.compile(r"[\s，。！？、,.!?;；:：~～…\-—\[\]()（）<>《》\"'“”‘’]+")
_REPLY_RE = re.compile(r"^\[回复<[^\]]*>.*?说[：:]\s*")
_REPLY2_RE = re.compile(r"^\[回复<[^\]]*>[：:]\s*")
_MENTION_RE = re.compile(r"^(?:@<[^>]*>|@\S+)\s*")
_URL_RE = re.compile(r"https?://\S+")
_MEDIA_RE = re.compile(r"^\[(表情包|图片|文件|动图|语音|视频|红包|转账|卡片|分享)")
_SCRIPT_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+")

_STOPWORDS = (
    "哪个好", "哪个", "哪些", "什么", "怎么", "怎样", "为什么", "是不是", "要不要", "有没有",
    "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里", "现在", "今天", "昨天",
    "明天", "刚才", "就是", "不是", "还是", "但是", "可是", "所以", "因为", "如果", "虽然",
    "而且", "然后", "已经", "可以", "真的", "一下", "一点", "有点", "非常", "特别", "比较",
    "应该", "可能", "知道", "觉得", "我们", "你们", "他们", "自己", "大家", "东西", "事情",
    "问题", "时候", "地方", "样子", "感觉", "请问", "帮忙", "帮我", "怎么选", "咋办",
)


def _clean(text: Any) -> str:
    """去掉引用前缀、@ 提及、链接，合并空白。"""
    value = str(text or "")
    value = _REPLY_RE.sub("", value)
    value = _REPLY2_RE.sub("", value)
    value = _MENTION_RE.sub("", value)
    marker = value.find("【转发消息】")
    if marker > 0:
        value = value[:marker]
    value = _URL_RE.sub(" ", value)
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


# 中文近义词组：命中任一个就带上同组其它词（弱权重），提升语义召回
_SYNONYM_GROUPS = [
    {"飞机", "飞机模型", "航模", "机", "飞行器"},
    {"电机", "马达", "驱动电机"},
    {"电池", "电芯", "锂电", "锂聚合物"},
    {"螺旋桨", "桨", "桨叶"},
    {"遥控", "摇控", "RC"},
    {"机翼", "翼", "翼面"},
    {"机身", "机体"},
    {"尾翼", "垂尾", "平尾", "尾轮"},
    {"起落架", "轮", "轮子"},
    {"3D", "三维", "3d"},
    {"陀螺仪", "陀螺", "飞控"},
    {"舵机", "伺服"},
    {"焊接", "焊", "锡焊"},
    {"碳纤维", "碳板", "碳管"},
    {"摔机", "坠机", "炸机"},
    {"重量", "减重", "轻量化"},
]

_SYNONYM_MAP: dict[str, set[str]] = {}
for _group in _SYNONYM_GROUPS:
    for _word in _group:
        _SYNONYM_MAP.setdefault(_word.lower(), set()).update(_group)


def _content_terms(query: str) -> set[str]:
    """剔除口语停用词后按中英文切分取词，避免跨字界产生垃圾词。"""
    text = str(query or "")
    for word in sorted(_STOPWORDS, key=len, reverse=True):
        text = text.replace(word, " ")
    terms: set[str] = set()
    for chunk in _SEGMENT_RE.split(text):
        for run in _SCRIPT_RE.findall(chunk):
            run = run.lower()
            if len(run) < 2:
                continue
            if run.isascii():
                terms.add(run)
                continue
            for size in (2, 3, 4):
                if len(run) < size:
                    break
                for start in range(len(run) - size + 1):
                    terms.add(run[start : start + size])
    return terms


def _fmt_age(ts: float) -> str:
    delta = max(0.0, time.time() - ts) if ts else 0.0
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


# ---------------------------------------------------------------- 数据读取 ----


def _db_candidates() -> List[Path]:
    paths: List[Path] = []
    env = os.environ.get("CAY_REMEMBER_DB")
    if env:
        paths.append(Path(env))
    try:
        paths.append(Path.cwd() / "data" / "MaiBot.db")
    except Exception:  # noqa: BLE001
        pass
    try:
        paths.append(Path(__file__).resolve().parents[2] / "data" / "MaiBot.db")
    except Exception:  # noqa: BLE001
        pass
    try:
        appdata = os.environ.get("APPDATA")
        if appdata:
            root = Path(appdata) / "MaiBotOneKeyDesktop"
            if root.is_dir():
                for child in root.iterdir():
                    candidate = child / "modules" / "MaiBot" / "data" / "MaiBot.db"
                    if candidate.is_file():
                        paths.append(candidate)
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    unique: List[Path] = []
    for p in paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _find_db_path() -> Optional[Path]:
    for p in _db_candidates():
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _parse_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _first(row: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _read_rows(limit: int, since: float = 0.0) -> List[Dict[str, Any]]:
    db_path = _find_db_path()
    if not db_path:
        raise FileNotFoundError("没有找到 MaiBot.db")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        params: List[Any] = []
        if "mai_messages" in tables:
            sql = "SELECT * FROM mai_messages"
            col = "timestamp"
        elif "messages" in tables:
            sql = "SELECT * FROM messages"
            col = "time"
        else:
            raise RuntimeError("数据库里没有消息表")
        numeric = False
        for r in con.execute(f"PRAGMA table_info({sql.split()[3]})"):
            if str(r[1]) == col:
                numeric = "INT" in str(r[2]).upper() or "REAL" in str(r[2]).upper() or "NUM" in str(r[2]).upper()
                break
        if since and since > 0 and numeric:
            sql += f" WHERE {col} >= ?"
            params.append(float(since))
        sql += f" ORDER BY {col} DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in con.execute(sql, tuple(params))]
    finally:
        con.close()


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chat_id": str(_first(row, ("chat_id", "session_id", "group_id"), "") or "").strip(),
        "text": str(_first(row, ("processed_plain_text", "plain_text", "raw_content", "content"), "") or "").strip(),
        "time": _parse_ts(_first(row, ("time", "timestamp", "received_at", "created_at"), 0)),
        "user_id": str(_first(row, ("user_id", "account_id"), "") or "").strip(),
        "user": str(_first(row, ("user_cardname", "user_nickname", "sender_name", "nickname"), "") or "").strip(),
        "group": str(_first(row, ("chat_info_group_name", "group_name"), "") or "").strip(),
        "is_command": bool(_first(row, ("is_command",), False)),
        "group_id": str(_first(row, ("group_id", "chat_info_group_id"), "") or "").strip(),
        "is_private": _first(row, ("group_id", "chat_info_group_id"), None) in (None, "")
        and _first(row, ("group_name", "chat_info_group_name"), None) in (None, ""),
    }


def _norm_decay(value: Any) -> str:
    """把双语选项或旧值统一成 window / curve。"""
    text = str(value or "").strip().lower()
    if "window" in text or "固定" in text:
        return "window"
    return "curve"


def _norm_group_mode(value: Any) -> str:
    """把双语选项或旧值统一成 all / none / whitelist / blacklist。"""
    text = str(value or "").strip().lower()
    if "whitelist" in text or "白名单" in text:
        return "whitelist"
    if "blacklist" in text or "黑名单" in text:
        return "blacklist"
    if "none" in text or "禁止" in text:
        return "none"
    return "all"


def _group_allowed(group_id: str, group_name: str, cfg: Any) -> bool:
    """按 all / none / whitelist / blacklist 判断某个群是否参与记忆。"""
    mode = _norm_group_mode(getattr(cfg.groups, "mode", "all"))
    if mode == "all":
        return True
    if mode == "none":
        return False
    rules = {str(x).strip() for x in (getattr(cfg.groups, "group_ids", []) or []) if str(x).strip()}
    if not rules:
        return mode == "blacklist"
    keys = {str(group_id or "").strip(), str(group_name or "").strip()} - {""}
    hit = bool(keys & rules)
    return hit if mode == "whitelist" else not hit


def _rank(
    query: str, candidates: List[Tuple[Dict[str, Any], str]], min_score: float
) -> List[Tuple[float, Dict[str, Any]]]:
    import math

    terms = _content_terms(query)
    if not terms or not candidates:
        return []

    # 语义扩展：同义词组带来弱权重候选词
    expanded: dict[str, float] = {t: 1.0 for t in terms}
    for term in terms:
        for synonym in _SYNONYM_MAP.get(term.lower(), ()):
            key = synonym.lower()
            if key not in expanded:
                expanded[key] = 0.6

    total = max(1, len(candidates))
    lowered = [(row, text.lower()) for row, text in candidates]

    df = {t: 0 for t in expanded}
    for _row, text in lowered:
        for term in expanded:
            if term in text:
                df[term] += 1

    active = {t for t in expanded if df[t] > 0}
    if not active:
        return []

    weight = {
        t: (math.log((total + 1) / (df[t] + 1)) + 0.05) * len(t) * expanded[t]
        for t in active
    }
    total_weight = sum(weight.values()) or 1.0

    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for row, text in lowered:
        hit = [t for t in active if t in text]
        # 子串模糊：三字以上的词只命中前两字（如「垂尾」vs「垂直尾翼」）算半分
        fuzzy = 0.0
        if len(hit) < len(active):
            for term in active:
                if term in hit or len(term) < 3:
                    continue
                head = term[:2]
                if head and head in text:
                    fuzzy += weight[term] * 0.5
        if not hit and fuzzy <= 0:
            continue
        score = (sum(weight[t] for t in hit) + fuzzy) / total_weight
        if score < min_score:
            continue
        ranked.append((score, row))
    return ranked


# ------------------------------------------------------------------ 插件 ----


class CAYRememberYou(MaiBotPlugin):
    """跨群记忆插件。"""

    config_model = CAYConfig

    def __init__(self) -> None:
        super().__init__()
        self._search_cache: Dict[str, Any] = {}
        self._injected_at: Dict[str, float] = {}
        self._bot_account = ""
        self._pending: Dict[str, Tuple[str, float]] = {}

    # ------------------------------------------------------------ 生命周期 --
    async def on_load(self) -> None:
        db = _find_db_path()
        self.ctx.logger.info(
            "跨群记忆已加载 v%s；数据库: %s", PLUGIN_VERSION, db if db else "未找到（仅工具/命令可用）"
        )

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        self.ctx.logger.info("跨群记忆配置已更新（scope=%s）", scope)

    async def on_unload(self) -> None:
        """插件卸载：无持久化状态，仅记录日志。"""
        self.ctx.logger.info("跨群记忆已卸载")

    # ---------------------------------------------------------------- 检索 --
    # ------------------------------------------------------- 向量语义（可选）--
    async def _embed(self, texts: List[str]) -> List[List[float]]:
        """用 MaiBot 的 embedding 任务取向量（会产生 embedding 消耗）。"""

        if not texts:
            return []
        fn = getattr(self.ctx.llm, "embed", None)
        if fn is None:
            return []
        try:
            result = await fn(texts=list(texts))
            vectors = self._vectors_from(result)
            if len(vectors) == len(texts):
                return vectors
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[跨群记忆] embedding 失败，退回纯关键词: %s", exc)
        return []

    @staticmethod
    def _vectors_from(result: Any) -> List[List[float]]:
        """从多种返回结构里取出向量列表。"""

        if isinstance(result, dict):
            for key in ("embeddings", "vectors", "data", "results", "embedding"):
                value = result.get(key)
                if isinstance(value, list):
                    if value and isinstance(value[0], dict):
                        return [list(x.get("embedding") or []) for x in value]
                    if value and isinstance(value[0], (int, float)):
                        return [list(value)]
                    return [list(x) for x in value if isinstance(x, list)]
        if isinstance(result, list):
            return [list(x) for x in result if isinstance(x, list)]
        return []

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    async def _semantic_rerank(self, query: str, ranked: List[Tuple[float, Dict[str, Any]]]) -> List[Tuple[float, Dict[str, Any]]]:
        """对关键词召回的前 N 条做向量重排，语义相近但字面不同的也能提上来。"""

        cfg = self.config.semantic
        if not cfg.enabled or not ranked:
            return ranked
        limit = max(4, min(int(cfg.rerank_limit), len(ranked)))
        head = ranked[:limit]
        tail = ranked[limit:]
        texts = [str(row.get("text") or "") for _s, row in head]
        vectors = await self._embed([query] + texts)
        if len(vectors) != len(texts) + 1:
            return ranked
        qv = vectors[0]
        blended: List[Tuple[float, Dict[str, Any]]] = []
        dropped: List[Tuple[float, Dict[str, Any]]] = []
        for (score, row), vec in zip(head, vectors[1:]):
            sim = self._cosine(qv, vec)
            if sim < float(cfg.min_similarity):
                # 语义不相关也不直接丢，降权保留，避免召回率下降
                dropped.append((float(score) * 0.5, row))
                continue
            blended.append((0.5 * float(score) + 0.5 * float(sim), row))
        blended.sort(key=lambda item: -item[0])
        dropped.sort(key=lambda item: -item[0])
        return blended + dropped + tail

    async def _search(
        self,
        query: str,
        current_chat_id: str = "",
        limit: int | None = None,
        is_private: bool = False,
        current_group: str = "",
    ) -> List[Dict[str, Any]]:
        cfg = self.config
        privacy = cfg.privacy
        if is_private and not privacy.private_as_target:
            return []
        if (not is_private) and current_group and not _group_allowed(current_group, current_group, cfg):
            return []
        query = _clean(query)
        if len(query) < 2:
            return []

        scan_limit = min(int(limit or cfg.memory.scan_limit), 5000)
        now = time.time()
        cache_key = f"{current_chat_id}|{int(is_private)}|{query[:80]}"
        cache_hit = self._search_cache.get(cache_key)
        if cache_hit and now - cache_hit[0] < 60:
            return list(cache_hit[1])
        window_mode = _norm_decay(cfg.memory.decay_mode) == "window"
        lookback_seconds = float(cfg.memory.lookback_days) * 86400
        since = now - lookback_seconds
        half_life = max(0.5, float(cfg.memory.half_life_days)) * 86400
        min_weight = float(cfg.memory.min_decay_weight)

        try:
            rows = [_normalize_row(r) for r in _read_rows(scan_limit, since)]
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("跨群记忆读取数据库失败: %s", exc)
            return []

        candidates: List[Tuple[Dict[str, Any], str]] = []
        for row in rows:
            text = _clean(row["text"])
            if len(text) < int(cfg.memory.min_message_chars):
                continue
            if _MEDIA_RE.match(text) or "file_ref:" in text:
                continue
            if row["time"]:
                age = now - row["time"]
                if age > lookback_seconds:
                    continue
                if (not window_mode) and min_weight > 0:
                    if 0.5 ** (age / half_life) < min_weight:
                        continue
            if privacy.blocked_stream_ids and row["chat_id"] in privacy.blocked_stream_ids:
                continue
            if row.get("is_private") and not privacy.private_as_source:
                continue
            if (not row.get("is_private")) and not _group_allowed(row.get("group_id", ""), row["group"], cfg):
                continue
            if current_chat_id and row["chat_id"] == current_chat_id:
                continue
            if cfg.memory.skip_commands and row["is_command"]:
                continue
            if cfg.memory.skip_bot_messages and self._bot_account and row["user_id"] == self._bot_account:
                continue
            candidates.append((row, text))

        if not candidates:
            return []

        try:
            ranked = _rank(query, candidates, 0.0)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("跨群记忆排序失败: %s", exc)
            return []

        if cfg.semantic.enabled:
            try:
                ranked = await self._semantic_rerank(query, ranked)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("跨群记忆向量重排失败（已忽略）: %s", exc)

        # 拟人记忆曲线：越久远的记忆越模糊（相关性 * 衰减系数）
        min_score = float(cfg.memory.min_score)
        weighted: List[Tuple[float, Dict[str, Any]]] = []
        for score, row in ranked:
            final = score
            if (not window_mode) and row.get("time"):
                weight = 0.5 ** (max(0.0, now - float(row["time"])) / half_life)
                final = score * (0.35 + 0.65 * weight)
            if final >= min_score:
                weighted.append((final, row))
        ranked = weighted

        ranked.sort(key=lambda item: (item[0], float(item[1].get("time") or 0.0)), reverse=True)

        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for score, row in ranked:
            text = _clean(row["text"])
            if text[:40] in seen:
                continue
            seen.add(text[:40])
            out.append(
                {
                    "score": round(float(score), 3),
                    "group": row["group"] or "某群",
                    "nickname": "某人" if cfg.privacy.mask_nicknames else (row["user"] or "某人"),
                    "text": text[: int(cfg.memory.max_chars_per_message)],
                    "time": float(row["time"] or 0.0),
                    "age": _fmt_age(float(row["time"] or 0.0)),
                }
            )
            if len(out) >= int(cfg.memory.max_snippets):
                break
        self._search_cache[cache_key] = (now, out)
        if len(self._search_cache) > 200:
            cutoff = now - 300
            for k in [k for k, v in self._search_cache.items() if v[0] < cutoff]:
                self._search_cache.pop(k, None)
        return out

    @staticmethod
    def _format(memories: List[Dict[str, Any]]) -> str:
        if not memories:
            return ""
        lines = ["【跨群记忆】（来自其他群聊，可自然地提及，不要暴露是数据库查的）"]
        for m in memories:
            lines.append(f"- [{m['group']}] {m['nickname']}（{m['age']}）：{m['text']}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 自动注入 --
    @HookHandler(
        "chat.receive.before_process",
        name="cross_group_memory_inject",
        description="按消息内容检索其他群的相关片段并追加到消息文本",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=4000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_memories(self, message: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        del kwargs
        cfg = self.config
        if message is None or not cfg.plugin.enabled or not cfg.memory.inject_enabled:
            return None

        text = self._message_text(message)
        if len(text) < 2 or text.lstrip().startswith("/"):
            return None

        stream_id = self._message_stream_id(message)
        memories = await self._search(
            text[-120:],
            stream_id,
            is_private=self._message_is_private(message),
            current_group=self._message_group_key(message),
        )
        if not memories:
            return None
        block = self._format(memories)
        if not block:
            return None

        # 不再改 message（会把记忆当作用户发言，导致答非所问/错群感）
        self._pending[stream_id] = (block, time.time())
        self.ctx.logger.debug('[跨群记忆] 已缓存 %s 条片段，等待注入提示词', len(memories))
        return None


    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="cross_group_memory_replyer",
        description="把本轮跨群记忆作为系统提示注入（不改用户消息）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def memory_replyer(self, **kwargs: Any) -> dict[str, Any]:
        return self._apply_pending(kwargs, "replyer")

    @HookHandler(
        "maisaka.planner.before_request",
        name="cross_group_memory_planner",
        description="把本轮跨群记忆作为系统提示注入（不改用户消息）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def memory_planner(self, **kwargs: Any) -> dict[str, Any]:
        return self._apply_pending(kwargs, "planner")

    def _apply_pending(self, kwargs: dict[str, Any], stage: str) -> dict[str, Any]:
        base = {"action": "continue", "modified_kwargs": kwargs}
        try:
            if not self.config.plugin.enabled or not self.config.memory.inject_enabled:
                return base
            session_id = str(kwargs.get("session_id") or "")
            if stage == "replyer":
                hit = self._pending.pop(session_id, None)
            else:
                hit = self._pending.get(session_id)
            if not hit:
                return base
            block, ts = hit
            now = time.time()
            if now - ts > 45:
                return base
            # 同一条记忆 10 分钟内不重复注入
            sig = f"{stage}:{hash(block)}"
            last = self._injected_at.get(sig)
            if last and now - last < 600:
                return base
            self._injected_at[sig] = now
            if len(self._injected_at) > 200:
                for k in [k for k, v in self._injected_at.items() if now - v > 600]:
                    self._injected_at.pop(k, None)
            # planner 只需要知道"有相关记忆"，给精简版省 token
            if stage == "planner":
                lines = block.splitlines()
                block = "\n".join(lines[:3]) + ("\n（更多细节留给回复阶段）" if len(lines) > 3 else "")
            target = _append_note(kwargs, block)
            if target:
                self.ctx.logger.info("[跨群记忆] 注入到 %s（%s）", stage, target)
            return base
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[跨群记忆] 注入失败（已忽略）: %s", exc)
            return base

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        for key in ("processed_plain_text", "plain_text", "text"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raw = message.get("raw_message")
        parts: List[str] = []
        if isinstance(raw, list):
            for seg in raw:
                if isinstance(seg, dict):
                    for key in ("text", "content"):
                        value = seg.get(key)
                        if isinstance(value, str):
                            parts.append(value)
        return _clean(" ".join(parts))

    @staticmethod
    def _message_group_key(message: dict[str, Any]) -> str:
        """当前群的标识（优先群号，其次群名）。"""
        info = message.get("message_info")
        if isinstance(info, dict):
            group = info.get("group_info")
            if isinstance(group, dict):
                return str(group.get("group_id") or group.get("group_name") or "")
        for key in ("group_id", "chat_info_group_id", "group_name"):
            value = message.get(key)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _message_is_private(message: dict[str, Any]) -> bool:
        """根据消息里的群信息判断是不是私聊。"""
        info = message.get("message_info")
        if isinstance(info, dict):
            group = info.get("group_info")
            if isinstance(group, dict) and group:
                return False
            if "group_info" in info:
                return True
        for key in ("group_id", "chat_info_group_id"):
            if message.get(key):
                return False
        return False

    @staticmethod
    def _message_stream_id(message: dict[str, Any]) -> str:
        for key in ("stream_id", "session_id", "chat_id"):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
        info = message.get("message_info") or {}
        if isinstance(info, dict):
            for key in ("stream_id", "session_id", "chat_id"):
                value = info.get(key)
                if isinstance(value, str) and value:
                    return value
            group = info.get("group_info") or {}
            if isinstance(group, dict):
                value = group.get("group_id")
                if value:
                    return str(value)
        return ""

    # ---------------------------------------------------------------- 工具 --
    @Tool(
        "search_cross_group_memory",
        description=(
            "跨群记忆检索 / Cross-group memory search：当需要回忆『别的群里聊过的内容』时使用。"
            "输入关键词、人名或短语，返回其他群中相关的历史消息片段。"
            "Search what other chats talked about by keyword, name or phrase."
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="关键词、人名或短语 / Keyword, name or phrase",
                required=True,
            )
        ],
    )
    async def tool_search(self, query: str = "", **kwargs: Any) -> dict[str, str]:
        stream_id = str(kwargs.get("stream_id") or "")
        memories = await self._search(query, stream_id)
        if not memories:
            return {"content": f"其他群里没有找到和「{query}」相关的内容"}
        return {"content": self._format(memories)}

    # ---------------------------------------------------------------- 命令 --
    @Command(
        "cross_group_memory",
        pattern=r"^/跨群记忆(?:\s+(?P<query>.+))?$",
        description="检索其他群聊里聊过的内容 / Search what other chats said; /跨群记忆 统计 for status",
    )
    async def cmd_memory(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        query = str(kwargs.get("query") or "").strip()

        if not query:
            await self.ctx.send.text(
                "用法：\n/跨群记忆 关键词 —— 检索其他群\n/跨群记忆 统计 —— 查看检索状态",
                stream_id,
            )
            return True, "显示用法", 1

        if query in {"统计", "stats"}:
            await self.ctx.send.text(await self._stats_text(), stream_id)
            return True, "输出统计", 1

        memories = await self._search(query, stream_id)
        if not memories:
            await self.ctx.send.text(f"其他群里没检索到「{query}」", stream_id)
            return True, "无结果", 1

        lines = [f"关于「{query}」在其他群里的记忆："]
        for m in memories:
            lines.append(f"· [{m['group']}] {m['nickname']}（{m['age']}）：{m['text']}")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"检索到 {len(memories)} 条", 1

    async def _stats_text(self) -> str:
        cfg = self.config
        lines = ["【跨群记忆 · 状态】"]
        try:
            rows = [_normalize_row(r) for r in _read_rows(500)]
            chats = len({r["chat_id"] for r in rows if r["chat_id"]})
            groups = len({r["group"] for r in rows if r["group"]})
            db_path = _find_db_path()
            lines.append(f"最近消息：{len(rows)} 条 / 聊天流 {chats} 个 / 群 {groups} 个")
            lines.append(f"数据库：{db_path.name if db_path else '未找到'}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"数据库：不可用（{exc}）")
        lines.append(
            f"自动注入：{'开' if cfg.memory.inject_enabled else '关'}"
            f" | 回溯 {cfg.memory.decay_mode}（{cfg.memory.lookback_days} 天上限 / 半衰期 {cfg.memory.half_life_days} 天）"
            f" | 扫描 {cfg.memory.scan_limit} 条"
            f" | 最多 {cfg.memory.max_snippets} 条"
            f" | 阈值 {cfg.memory.min_score}"
        )
        privacy = cfg.privacy
        lines.append(
            f"隐私：私聊可被检索={'开' if privacy.private_as_source else '关'}"
            f" | 私聊可收记忆={'开' if privacy.private_as_target else '关'}"
            f" | 隐藏昵称={'开' if privacy.mask_nicknames else '关'}"
            f" | 排除会话={len(privacy.blocked_stream_ids) or 0} 个"
        )
        groups_cfg = cfg.groups
        lines.append(f"群组模式：{groups_cfg.mode} | 名单 {len(groups_cfg.group_ids or [])} 个")
        return "\n".join(lines)


def create_plugin() -> MaiBotPlugin:
    """插件工厂函数，供 Runner 调用。"""

    return CAYRememberYou()

def _append_note(kwargs: dict[str, Any], note: str) -> str:
    """把记忆写进 hook 参数（system item 或 extra_prompt），返回写入位置。"""

    items = kwargs.get("items")
    if isinstance(items, list):
        for item in reversed(items):
            if isinstance(item, dict) and item.get("item_type") == "system":
                parts = item.get("parts")
                if not isinstance(parts, list):
                    parts = []
                    item["parts"] = parts
                parts.append({"type": "text", "text": note})
                return "items"
        items.insert(0, {
            "item_type": "system",
            "meta": {
                "item_id": "CAYRemember-{}".format(datetime.now().timestamp()),
                "logical_turn_id": None,
                "timestamp": datetime.now().astimezone().isoformat(),
            },
            "parts": [{"type": "text", "text": note}],
        })
        return "items"
    if "extra_prompt" in kwargs:
        kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + note
        return "extra_prompt"
    return ""
