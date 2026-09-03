# -*- coding: utf-8 -*-
"""QQ 群聊 AI 日报核心逻辑。

当前安全模式只读打开由 nt_msg_db_util 等工具生成的 nt_msg_export.db，
不会附加、注入或修改正在运行的 QQ 进程。
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable

import requests


APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
DEFAULT_PROVIDER = "deepseek"
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "default_model": "deepseek-v4-flash",
    },
    "nvidia": {
        "label": "NVIDIA API Catalog",
        "endpoint": "https://integrate.api.nvidia.com/v1/chat/completions",
        "default_model": "deepseek-ai/deepseek-v4-pro-0813",
    },
}
# 保留旧常量，避免已有调用方失效。
DEFAULT_MODEL = PROVIDERS[DEFAULT_PROVIDER]["default_model"]
SUMMARY_CHUNK_CHARS = 18000


class QQSummaryError(RuntimeError):
    """展示给用户的可读错误。"""


@dataclass(frozen=True)
class GroupInfo:
    key: str
    display_name: str
    message_count: int
    first_timestamp: int
    last_timestamp: int


class AliasBook:
    """可选的本地群名/群成员昵称映射，不会发送 QQ 号给模型。"""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.groups = {
            str(key): str(value).strip()
            for key, value in (data.get("groups") or {}).items()
            if str(value).strip()
        }
        self.members = {}
        for group_key, mapping in (data.get("members") or {}).items():
            if not isinstance(mapping, dict):
                continue
            self.members[str(group_key)] = {
                str(key): str(value).strip()
                for key, value in mapping.items()
                if str(value).strip()
            }
        self.global_members = {
            str(key): str(value).strip()
            for key, value in (data.get("global_members") or {}).items()
            if str(value).strip()
        }

    @classmethod
    def load(cls, path: str | os.PathLike | None) -> "AliasBook":
        if not path:
            return cls()
        alias_path = Path(path)
        if not alias_path.is_file():
            return cls()
        try:
            with alias_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise QQSummaryError(f"昵称映射文件读取失败：{exc}") from exc
        if not isinstance(data, dict):
            raise QQSummaryError("昵称映射文件格式错误：最外层必须是 JSON 对象。")
        return cls(data)

    def group_name(self, group_key: str) -> str:
        return self.groups.get(str(group_key), "")

    def member_name(self, group_key: str, member_key: str) -> str:
        member_key = str(member_key)
        return (
            self.members.get(str(group_key), {}).get(member_key)
            or self.global_members.get(member_key)
            or ""
        )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value)


def _normal_timestamp(value) -> int:
    timestamp = int(value)
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp


class QQExportDatabase:
    REQUIRED_COLUMNS = {"timestamp", "sender_qq", "sender_uid", "group_qq", "group_id", "text"}
    OPTIONAL_SENDER_COLUMNS = (
        "sender_card",
        "sender_name",
        "sender_nickname",
        "nickname",
    )
    OPTIONAL_GROUP_COLUMNS = ("group_name", "group_title")

    def __init__(self, path: str | os.PathLike, aliases: AliasBook | None = None):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise QQSummaryError("没有找到所选数据库文件。")
        self.aliases = aliases or AliasBook()
        uri = self.path.as_uri() + "?mode=ro"
        try:
            self.connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.columns = self._validate_schema()
        except sqlite3.Error as exc:
            raise QQSummaryError(f"无法只读打开数据库：{exc}") from exc

    def close(self):
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
            self.connection = None

    def _validate_schema(self) -> set[str]:
        table = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='group_messages'"
        ).fetchone()
        if not table:
            raise QQSummaryError(
                "这不是受支持的 QQ 导出库：缺少 group_messages 表。"
                "请先生成 nt_msg_export.db。"
            )
        columns = {
            row[1] for row in self.connection.execute('PRAGMA table_info("group_messages")')
        }
        missing = sorted(self.REQUIRED_COLUMNS - columns)
        if missing:
            raise QQSummaryError(
                "QQ 导出库缺少必要字段：" + "、".join(missing)
            )
        return columns

    @staticmethod
    def _group_key_expression() -> str:
        return (
            "COALESCE(NULLIF(CAST(group_qq AS TEXT), '0'), "
            "NULLIF(CAST(group_id AS TEXT), ''))"
        )

    def list_groups(self) -> list[GroupInfo]:
        group_expr = self._group_key_expression()
        optional_name = next(
            (column for column in self.OPTIONAL_GROUP_COLUMNS if column in self.columns),
            None,
        )
        name_expr = (
            f", MAX({_quote_identifier(optional_name)}) AS database_group_name"
            if optional_name
            else ", NULL AS database_group_name"
        )
        sql = f"""
            SELECT {group_expr} AS group_key,
                   COUNT(*) AS message_count,
                   MIN(timestamp) AS first_timestamp,
                   MAX(timestamp) AS last_timestamp
                   {name_expr}
            FROM group_messages
            WHERE text IS NOT NULL
              AND TRIM(CAST(text AS TEXT)) <> ''
              AND {group_expr} IS NOT NULL
            GROUP BY group_key
            ORDER BY message_count DESC
        """
        groups = []
        for row in self.connection.execute(sql):
            key = _text(row["group_key"]).strip()
            database_name = _text(row["database_group_name"]).strip()
            display_name = self.aliases.group_name(key) or database_name or f"QQ群 {key}"
            groups.append(
                GroupInfo(
                    key=key,
                    display_name=display_name,
                    message_count=int(row["message_count"]),
                    first_timestamp=_normal_timestamp(row["first_timestamp"]),
                    last_timestamp=_normal_timestamp(row["last_timestamp"]),
                )
            )
        return groups

    def get_messages(self, group_key: str, start_ts: int, end_ts: int) -> list[str]:
        group_expr = self._group_key_expression()
        optional_sender_columns = [
            column for column in self.OPTIONAL_SENDER_COLUMNS if column in self.columns
        ]
        optional_sql = "".join(
            f", {_quote_identifier(column)}" for column in optional_sender_columns
        )
        sql = f"""
            SELECT timestamp, sender_qq, sender_uid, text {optional_sql}
            FROM group_messages
            WHERE {group_expr} = ?
              AND timestamp >= ?
              AND timestamp <= ?
              AND text IS NOT NULL
              AND TRIM(CAST(text AS TEXT)) <> ''
            ORDER BY timestamp ASC, rowid ASC
        """
        rows = list(self.connection.execute(sql, (str(group_key), int(start_ts), int(end_ts))))
        if not rows:
            return []

        prepared = []
        database_names = {}
        for row in rows:
            sender_qq = _text(row["sender_qq"]).strip()
            sender_uid = _text(row["sender_uid"]).strip()
            sender_key = sender_qq if sender_qq and sender_qq != "0" else sender_uid
            if not sender_key:
                sender_key = "unknown"
            for column in optional_sender_columns:
                candidate = _text(row[column]).strip()
                if candidate:
                    database_names.setdefault(sender_key, candidate)
                    break
            content = _text(row["text"]).strip()
            if content:
                prepared.append((_normal_timestamp(row["timestamp"]), sender_key, content))

        senders = sorted({sender for _timestamp, sender, _content in prepared})
        base_names = {}
        for index, sender in enumerate(senders, start=1):
            base_names[sender] = (
                self.aliases.member_name(str(group_key), sender)
                or database_names.get(sender)
                or f"群友{index}"
            )

        same_name_groups: dict[str, list[str]] = {}
        for sender, name in base_names.items():
            clean_name = re.sub(r"[\r\n\t]+", " ", name).strip() or "未知成员"
            base_names[sender] = clean_name
            same_name_groups.setdefault(clean_name, []).append(sender)

        labels = {}
        for name, same_name_senders in same_name_groups.items():
            ordered = sorted(same_name_senders)
            if len(ordered) == 1:
                labels[ordered[0]] = name
            else:
                for index, sender in enumerate(ordered, start=1):
                    labels[sender] = f"{name}（同名{index}）"

        return [
            f"[{dt.datetime.fromtimestamp(timestamp):%Y-%m-%d %H:%M}] "
            f"{labels.get(sender, '未知成员')}：{content}"
            for timestamp, sender, content in prepared
        ]


def find_running_nt_msg_databases() -> list[Path]:
    """只读查询 QQ 进程已打开的 nt_msg.db，不扫描或修改磁盘。"""
    try:
        import psutil
    except ImportError:
        return []
    paths = set()
    for process in psutil.process_iter(["name"]):
        name = (process.info.get("name") or "").lower()
        if name != "qq.exe":
            continue
        try:
            for opened in process.open_files():
                if opened.path.lower().endswith("nt_msg.db"):
                    paths.add(Path(opened.path))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return sorted(paths)


DEFAULT_PROMPT_TEMPLATE = """\
你是一个熟悉群内气氛、会接梗但不冒犯人的 QQ 群聊日报编辑。
请根据下面的完整聊天记录，生成一份可直接复制发送到 QQ 群的纯文本日报。

时间范围：{date_range}
消息数量：{count} 条

{messages}

每行严格采用“[时间] 发送者：消息正文”的格式。发送者标签由数据库生成；
正文里出现的其他昵称只是被提及、回复或引用的人，绝不能当成当前发言人。

请输出：
🗞 今日概览
用一段话概括群聊气氛和主要内容。

🔥 核心议题
选出 3～6 个主要话题，用 1️⃣、2️⃣、3️⃣ 编号。故事线清晰时写明起因、发展、讨论和结果。

👑 今日 MVP
写“昵称｜有节目效果的称号”，再用一句话说明原因。只依据该发送者自己的发言。

🏆 趣味成就
颁发 5～8 个基于真实聊天的搞笑成就：
① 成就名称｜昵称
理由：简短、有梗但不恶意的说明

要求：
1. 只输出纯文本，禁止 Markdown 标题、加粗、表格、代码块和 Markdown 链接。
2. 自然穿插 emoji 和最多 3 处表情包式旁白，轻松诙谐但不要堆砌。
3. 涉及个人观点、人设、MVP 或成就时，必须以行首发送者为准；拿不准就写“有群友提到”。
4. 不得杜撰，不得恶意攻击，不要重复输出群号、日期和消息数。
"""


def _split_chunks(items: Iterable[str], max_chars: int = SUMMARY_CHUNK_CHARS) -> list[str]:
    chunks = []
    current = []
    size = 0
    for original in items:
        text = str(original)
        parts = [text[index:index + max_chars] for index in range(0, len(text), max_chars)] or [""]
        for part in parts:
            extra = len(part) + (1 if current else 0)
            if current and size + extra > max_chars:
                chunks.append("\n".join(current))
                current = []
                size = 0
            current.append(part)
            size += len(part) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def provider_label(provider: str) -> str:
    config = PROVIDERS.get(provider)
    return str(config["label"]) if config else provider


def provider_default_model(provider: str) -> str:
    config = PROVIDERS.get(provider)
    if not config:
        raise QQSummaryError(f"不支持的 AI 服务商：{provider}")
    return str(config["default_model"])


def _chat_completion(
    api_key: str,
    prompt: str,
    max_tokens: int = 2200,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
) -> str:
    provider_config = PROVIDERS.get(provider)
    if not provider_config:
        raise QQSummaryError(f"不支持的 AI 服务商：{provider}")
    label = str(provider_config["label"])
    if not api_key.strip():
        raise QQSummaryError(f"请先填写 {label} API Key。")
    selected_model = (model or "").strip() or str(provider_config["default_model"])
    try:
        response = requests.post(
            str(provider_config["endpoint"]),
            headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
            json={
                "model": selected_model,
                "messages": [
                    {"role": "system", "content": "你擅长准确提炼群聊，绝不混淆发言人。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.35,
                "max_tokens": max_tokens,
            },
            timeout=90,
        )
    except requests.RequestException as exc:
        raise QQSummaryError(f"连接 {label} API 失败：{exc}") from exc

    if provider == "nvidia":
        error_messages = {
            400: "NVIDIA 请求格式错误，请检查模型名是否正确。",
            401: "NVIDIA API Key 无效，请在 API Catalog 重新生成并完整复制。",
            402: "NVIDIA API 免费额度或试用权益不可用，请检查账号状态。",
            403: "NVIDIA 拒绝了请求，当前 Key 可能没有该模型的访问权限。",
            404: "NVIDIA 没有找到该模型，请从模型页重新复制模型名。",
            422: "NVIDIA 不接受当前请求参数，请检查模型名。",
            429: "NVIDIA 免费接口达到频率或额度限制，请稍后再试。",
            500: "NVIDIA 服务暂时异常，请稍后再试。",
            503: "NVIDIA 当前繁忙，请稍后再试。",
        }
    else:
        error_messages = {
            400: "DeepSeek 请求格式错误，请更新工具后重试。",
            401: "DeepSeek API Key 无效，请检查是否复制完整。",
            402: "DeepSeek API 余额不足，请充值或更换有余额的 API Key。",
            422: "DeepSeek 请求参数不兼容，请更新工具后重试。",
            429: "DeepSeek 请求过于频繁，请稍后再试。",
            500: "DeepSeek 服务暂时异常，请稍后再试。",
            503: "DeepSeek 当前繁忙，请稍后再试。",
        }
    if response.status_code in error_messages:
        raise QQSummaryError(error_messages[response.status_code])
    try:
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("响应中没有文本内容")
        return content
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        raise QQSummaryError(f"{label} 返回了无法识别的响应：{exc}") from exc


def _deepseek_chat(api_key: str, prompt: str, max_tokens: int = 2200) -> str:
    """向后兼容的 DeepSeek 调用入口。"""
    return _chat_completion(api_key, prompt, max_tokens=max_tokens, provider="deepseek")


def _markdown_table_to_text(lines: list[str]) -> list[str]:
    converted = []
    index = 0
    separator = re.compile(r"^:?-{3,}:?$")
    while index < len(lines):
        if index + 1 < len(lines) and "|" in lines[index] and "|" in lines[index + 1]:
            dividers = [cell.strip().replace(" ", "") for cell in lines[index + 1].strip().strip("|").split("|")]
            if dividers and all(separator.fullmatch(cell) for cell in dividers):
                index += 2
                row_number = 1
                while index < len(lines) and "|" in lines[index]:
                    cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    converted.append(f"{row_number}、" + "｜".join(cells))
                    row_number += 1
                    index += 1
                continue
        converted.append(lines[index])
        index += 1
    return converted


def to_plain_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_markdown_table_to_text(text.split("\n")))
    text = re.sub(r"(?m)^\s*```[^\n]*$", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ai_summarize(
    messages: list[str],
    api_key: str,
    date_range: str,
    prompt_template: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
) -> str:
    if not messages:
        return "该时间段内没有文本消息。"
    template = prompt_template or DEFAULT_PROMPT_TEMPLATE
    try:
        template.format(date_range=date_range, count=len(messages), messages="")
    except KeyError as exc:
        raise QQSummaryError(f"提示词包含未知占位符：{exc}") from exc

    def notify(message: str):
        if progress_callback:
            try:
                progress_callback(message)
            except Exception:
                pass

    chunks = _split_chunks(messages)
    if len(chunks) == 1:
        notify(f"正在通过 {provider_label(provider)} 生成 AI 总结...")
        prompt = template.format(date_range=date_range, count=len(messages), messages=chunks[0])
        return to_plain_text(
            _chat_completion(api_key, prompt, provider=provider, model=model)
        )

    partials = []
    for index, chunk in enumerate(chunks, start=1):
        notify(f"正在提炼第 {index}/{len(chunks)} 段聊天记录...")
        partials.append(
            _chat_completion(
                api_key,
                "每行格式为“[时间] 发送者：正文”。请提炼下面这段 QQ 群聊的核心话题、事实和待办，"
                "保留已经明确对应的发送者；被提及或引用的人不是当前发言人，证据不足就不要署名。\n\n"
                + chunk,
                max_tokens=1400,
                provider=provider,
                model=model,
            )
        )

    notify("正在合并分段摘要...")
    merged = "以下是完整聊天记录的分段提炼结果，请去重并综合：\n\n" + "\n\n".join(partials)
    prompt = template.format(date_range=date_range, count=len(messages), messages=merged)
    return to_plain_text(
        _chat_completion(api_key, prompt, provider=provider, model=model)
    )


def load_config() -> dict:
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict):
    with CONFIG_FILE.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
