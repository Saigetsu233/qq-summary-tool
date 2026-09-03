# -*- coding: utf-8 -*-
"""QQ 群聊 AI 日报图形界面。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from tkcalendar import DateEntry
except ImportError as exc:
    raise SystemExit("缺少 tkcalendar，请先运行：pip install -r requirements.txt") from exc

from qq_summary import (
    APP_DIR,
    AliasBook,
    DEFAULT_PROVIDER,
    DEFAULT_PROMPT_TEMPLATE,
    GroupInfo,
    PROVIDERS,
    QQExportDatabase,
    QQSummaryError,
    ai_summarize,
    find_running_nt_msg_databases,
    load_config,
    provider_default_model,
    provider_label,
    save_config,
)


class QQSummaryApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QQ 群聊 AI 总结工具")
        self.root.geometry("850x880")
        self.root.minsize(720, 780)

        self.database: QQExportDatabase | None = None
        self.groups: list[GroupInfo] = []
        self.config = load_config()
        self.prompt_template = self.config.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE

        configured_provider = str(self.config.get("provider") or DEFAULT_PROVIDER)
        if configured_provider not in PROVIDERS:
            configured_provider = DEFAULT_PROVIDER
        stored_keys = self.config.get("api_keys")
        self.provider_keys = dict(stored_keys) if isinstance(stored_keys, dict) else {}
        # 自动迁移旧版单个 DeepSeek Key 配置。
        if self.config.get("api_key") and not self.provider_keys.get("deepseek"):
            self.provider_keys["deepseek"] = str(self.config["api_key"])
        stored_models = self.config.get("models")
        self.provider_models = dict(stored_models) if isinstance(stored_models, dict) else {}
        self.current_provider = configured_provider

        self.db_path_var = tk.StringVar(value="尚未选择 nt_msg_export.db")
        self.group_var = tk.StringVar()
        self.provider_var = tk.StringVar(value=provider_label(configured_provider))
        self.api_key_var = tk.StringVar(
            value=str(self.provider_keys.get(configured_provider, ""))
        )
        self.model_var = tk.StringVar(
            value=str(
                self.provider_models.get(configured_provider)
                or provider_default_model(configured_provider)
            )
        )
        self.status_var = tk.StringVar(value="请选择 QQ 导出数据库")
        self.message_count_var = tk.StringVar()
        self.show_key_var = tk.BooleanVar(value=False)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        source = ttk.LabelFrame(self.root, text="第一步：选择 QQ 数据（只读安全模式）")
        source.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(source, textvariable=self.db_path_var, foreground="#087f23").pack(
            fill="x", padx=10, pady=(8, 4)
        )
        source_buttons = ttk.Frame(source)
        source_buttons.pack(fill="x", padx=10, pady=(2, 8))
        self.auto_button = ttk.Button(
            source_buttons, text="自动查找", command=self._on_auto_find, width=14
        )
        self.auto_button.pack(side="left")
        self.select_button = ttk.Button(
            source_buttons, text="选择导出数据库", command=self._on_select_database, width=18
        )
        self.select_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            source_buttons,
            text="支持 nt_msg_db_util 生成的 nt_msg_export.db",
            foreground="gray",
        ).pack(side="left", padx=(12, 0))

        group_frame = ttk.LabelFrame(self.root, text="第二步：选择群聊")
        group_frame.pack(fill="x", padx=12, pady=6)
        self.group_combo = ttk.Combobox(
            group_frame, textvariable=self.group_var, state="disabled", width=70
        )
        self.group_combo.pack(fill="x", padx=10, pady=10)

        date_frame = ttk.LabelFrame(self.root, text="第三步：选择时间范围")
        date_frame.pack(fill="x", padx=12, pady=6)
        today = dt.date.today()
        ttk.Label(date_frame, text="开始日期：").pack(side="left", padx=(10, 2), pady=10)
        self.start_date = DateEntry(
            date_frame, width=12, date_pattern="yyyy-mm-dd", maxdate=today
        )
        self.start_date.set_date(today)
        self.start_date.pack(side="left", padx=(0, 16), pady=10)
        ttk.Label(date_frame, text="结束日期：").pack(side="left", padx=(0, 2), pady=10)
        self.end_date = DateEntry(
            date_frame, width=12, date_pattern="yyyy-mm-dd", maxdate=today
        )
        self.end_date.set_date(today)
        self.end_date.pack(side="left", pady=10)

        action_row = ttk.Frame(self.root)
        action_row.pack(fill="x", padx=12, pady=6)
        self.summarize_button = ttk.Button(
            action_row, text="生成总结", command=self._on_summarize, state="disabled", width=16
        )
        self.summarize_button.pack(side="left")
        ttk.Button(
            action_row, text="修改提示词", command=self._on_edit_prompt, width=14
        ).pack(side="left", padx=(8, 0))
        ttk.Label(action_row, textvariable=self.message_count_var).pack(side="left", padx=12)
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=130)
        self.progress.pack(side="right")

        result_frame = ttk.LabelFrame(self.root, text="总结结果")
        result_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.result_text = scrolledtext.ScrolledText(
            result_frame, wrap="word", font=("微软雅黑", 10), height=20
        )
        self.result_text.pack(fill="both", expand=True, padx=8, pady=8)
        result_buttons = ttk.Frame(result_frame)
        result_buttons.pack(pady=(0, 8))
        ttk.Button(result_buttons, text="复制到剪贴板", command=self._on_copy, width=16).pack(
            side="left", padx=6
        )
        ttk.Button(result_buttons, text="保存为 TXT", command=self._on_save, width=16).pack(
            side="left", padx=6
        )

        self.api_frame = ttk.LabelFrame(self.root, text="第四步：选择 AI 服务")
        self.api_frame.pack(fill="x", padx=12, pady=6)

        provider_row = ttk.Frame(self.api_frame)
        provider_row.pack(fill="x", padx=10, pady=(8, 3))
        ttk.Label(provider_row, text="服务商：").pack(side="left")
        self.provider_combo = ttk.Combobox(
            provider_row,
            textvariable=self.provider_var,
            values=[provider_label(key) for key in PROVIDERS],
            state="readonly",
            width=23,
        )
        self.provider_combo.pack(side="left", padx=(2, 14))
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)
        ttk.Label(provider_row, text="模型：").pack(side="left")
        ttk.Entry(provider_row, textvariable=self.model_var).pack(
            side="left", fill="x", expand=True, padx=(2, 0)
        )

        key_row = ttk.Frame(self.api_frame)
        key_row.pack(fill="x", padx=10, pady=(3, 2))
        ttk.Label(key_row, text="API Key：").pack(side="left")
        self.api_entry = ttk.Entry(key_row, textvariable=self.api_key_var, show="*")
        self.api_entry.pack(side="left", fill="x", expand=True, padx=(2, 6))
        ttk.Checkbutton(
            key_row, text="显示", variable=self.show_key_var, command=self._toggle_key
        ).pack(side="left")
        ttk.Label(
            self.api_frame,
            text="NVIDIA API Catalog 提供原型阶段免费接口，可用性、频率和额度以 NVIDIA 账号页面为准。",
            foreground="gray",
        ).pack(anchor="w", padx=10, pady=(2, 7))

        status = ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", relief="sunken"
        )
        status.pack(fill="x", side="bottom")

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.auto_button.configure(state=state)
        self.select_button.configure(state=state)
        if busy:
            self.summarize_button.configure(state="disabled")
            self.progress.start(10)
        else:
            self.summarize_button.configure(state="normal" if self.database else "disabled")
            self.progress.stop()

    def _set_status(self, message: str):
        self.root.after(0, lambda: self.status_var.set(message))

    def _provider_key_from_label(self, label: str) -> str:
        for key, config in PROVIDERS.items():
            if config["label"] == label:
                return key
        return DEFAULT_PROVIDER

    def _remember_provider_settings(self):
        self.provider_keys[self.current_provider] = self.api_key_var.get().strip()
        self.provider_models[self.current_provider] = self.model_var.get().strip()

    def _on_provider_changed(self, _event=None):
        self._remember_provider_settings()
        selected = self._provider_key_from_label(self.provider_var.get())
        self.current_provider = selected
        self.api_key_var.set(str(self.provider_keys.get(selected, "")))
        self.model_var.set(
            str(self.provider_models.get(selected) or provider_default_model(selected))
        )
        self.status_var.set(f"已切换到 {provider_label(selected)}")

    def _on_auto_find(self):
        candidates = []
        saved = self.config.get("database_path")
        if saved and Path(saved).is_file():
            candidates.append(Path(saved))
        local_export = APP_DIR / "nt_msg_export.db"
        if local_export.is_file():
            candidates.append(local_export)
        if candidates:
            self._load_database(candidates[0])
            return

        raw_databases = find_running_nt_msg_databases()
        if raw_databases:
            messagebox.showinfo(
                "已找到 NTQQ 数据库",
                "检测到 QQ 正在使用加密的 nt_msg.db，但没有发现 nt_msg_export.db。\n\n"
                "安全模式不会附加或修改 QQ 进程。请先按照 README 使用 nt_msg_db_util 生成导出库，"
                "再点击“选择导出数据库”。",
            )
            self.status_var.set("已找到原始加密库，等待选择 nt_msg_export.db")
        else:
            messagebox.showwarning(
                "没有找到数据库",
                "请保持 NTQQ 登录，或直接点击“选择导出数据库”选择 nt_msg_export.db。",
            )

    def _on_select_database(self):
        path = filedialog.askopenfilename(
            title="选择 NTQQ 导出数据库",
            filetypes=[("QQ 导出数据库", "*.db"), ("所有文件", "*.*")],
        )
        if path:
            self._load_database(Path(path))

    def _load_database(self, path: Path):
        self._set_busy(True)
        self.status_var.set("正在只读检查 QQ 导出数据库...")

        def worker():
            new_database = None
            try:
                alias_path = path.with_name("aliases.json")
                aliases = AliasBook.load(alias_path if alias_path.is_file() else None)
                new_database = QQExportDatabase(path, aliases=aliases)
                groups = new_database.list_groups()
                if not groups:
                    raise QQSummaryError("导出库中没有可用的群聊文本消息。")
                old_database = self.database
                self.database = new_database
                new_database = None
                self.groups = groups
                if old_database:
                    old_database.close()
                values = [f"{group.display_name}（{group.message_count} 条文本）" for group in groups]

                def update_ui():
                    self.db_path_var.set(f"数据源：{path}")
                    self.group_combo.configure(values=values, state="readonly")
                    self.group_combo.current(0)
                    self.status_var.set(f"加载完成，共 {len(groups)} 个群聊")
                    self._set_busy(False)

                self.root.after(0, update_ui)
            except Exception as exc:
                if new_database:
                    new_database.close()
                message = str(exc)

                def show_error():
                    self._set_busy(False)
                    self.status_var.set("数据库加载失败")
                    messagebox.showerror("加载失败", message)

                self.root.after(0, show_error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_summarize(self):
        if not self.database:
            messagebox.showwarning("提示", "请先选择 QQ 导出数据库。")
            return
        index = self.group_combo.current()
        if index < 0 or index >= len(self.groups):
            messagebox.showwarning("提示", "请选择一个群聊。")
            return
        start_date = self.start_date.get_date()
        end_date = self.end_date.get_date()
        if start_date > end_date:
            messagebox.showwarning("日期错误", "开始日期不能晚于结束日期。")
            return
        self._remember_provider_settings()
        provider = self.current_provider
        api_key = self.provider_keys.get(provider, "").strip()
        model = self.provider_models.get(provider, "").strip()
        if not api_key:
            messagebox.showwarning("提示", f"请先填写 {provider_label(provider)} API Key。")
            return
        if not model:
            messagebox.showwarning("提示", "请先填写模型名。")
            return

        group = self.groups[index]
        start_ts = int(dt.datetime.combine(start_date, dt.time.min).timestamp())
        end_ts = int(dt.datetime.combine(end_date, dt.time.max).timestamp())
        self._set_busy(True)
        self.result_text.delete("1.0", "end")
        self.message_count_var.set("")
        self.status_var.set("正在读取群聊文本...")

        def worker():
            try:
                messages = self.database.get_messages(group.key, start_ts, end_ts)
                if not messages:
                    raise QQSummaryError("该时间段内没有文本消息。")
                count = len(messages)
                self.root.after(
                    0, lambda: self.message_count_var.set(f"共 {count} 条文本消息")
                )
                date_range = f"{start_date} 至 {end_date}"
                summary = ai_summarize(
                    messages,
                    api_key,
                    date_range,
                    prompt_template=self.prompt_template,
                    progress_callback=self._set_status,
                    provider=provider,
                    model=model,
                )
                header = (
                    f"群聊：{group.display_name}\n"
                    f"时间：{date_range}（共 {count} 条消息）\n"
                    f"{'─' * 36}\n"
                )
                output = header + summary

                def show_result():
                    self.result_text.insert("1.0", output)
                    self.status_var.set("总结完成")
                    self._set_busy(False)

                self.root.after(0, show_result)
            except Exception as exc:
                message = str(exc)

                def show_error():
                    self.status_var.set("生成失败")
                    self._set_busy(False)
                    messagebox.showerror("生成失败", message)

                self.root.after(0, show_error)

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_key(self):
        self.api_entry.configure(show="" if self.show_key_var.get() else "*")

    def _on_copy(self):
        text = self.result_text.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set("已复制到剪贴板")

    def _on_save(self):
        text = self.result_text.get("1.0", "end").strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(
            title="保存总结",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile=f"QQ群聊总结_{dt.datetime.now():%Y%m%d_%H%M}.txt",
        )
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.status_var.set("总结已保存")

    def _on_edit_prompt(self):
        window = tk.Toplevel(self.root)
        window.title("修改提示词")
        window.geometry("680x600")
        ttk.Label(
            window,
            text="可用占位符：{date_range}、{count}、{messages}（必须保留）",
            foreground="gray",
        ).pack(anchor="w", padx=12, pady=(10, 4))
        editor = scrolledtext.ScrolledText(window, wrap="word", font=("微软雅黑", 10))
        editor.pack(fill="both", expand=True, padx=12, pady=4)
        editor.insert("1.0", self.prompt_template)
        buttons = ttk.Frame(window)
        buttons.pack(pady=10)

        def save():
            value = editor.get("1.0", "end").rstrip()
            if "{messages}" not in value:
                messagebox.showwarning("格式错误", "提示词必须包含 {messages}。", parent=window)
                return
            self.prompt_template = value
            self.status_var.set("提示词已更新")
            window.destroy()

        def reset():
            editor.delete("1.0", "end")
            editor.insert("1.0", DEFAULT_PROMPT_TEMPLATE)

        ttk.Button(buttons, text="保存", command=save, width=12).pack(side="left", padx=5)
        ttk.Button(buttons, text="恢复默认", command=reset, width=12).pack(side="left", padx=5)
        ttk.Button(buttons, text="取消", command=window.destroy, width=12).pack(side="left", padx=5)

    def _on_close(self):
        self._remember_provider_settings()
        config = {
            "provider": self.current_provider,
            "api_keys": self.provider_keys,
            "models": self.provider_models,
            "prompt_template": self.prompt_template,
        }
        if self.database:
            config["database_path"] = str(self.database.path)
            self.database.close()
        save_config(config)
        self.root.destroy()


def main():
    root = tk.Tk()
    QQSummaryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
