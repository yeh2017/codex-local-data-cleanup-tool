import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .history import (
    HistoryRecord,
    delete_history_records,
    scan_history_records,
)
from .history_backup import (
    BackupSafetyError,
    create_history_backup,
    ensure_backup_root,
    migrate_backup_root,
    restore_history_backup,
)
from .i18n import (
    ENGLISH,
    LANGUAGE_LABELS,
    SIMPLIFIED_CHINESE,
    Translator,
    resolve_language,
)
from .log_maintenance import (
    LogDiagnostics,
    LogGrowthCancelled,
    LogSafetyError,
    classify_log_growth,
    inspect_logs,
    optimize_logs,
    sample_log_growth,
)
from .models import ScanItem, ScanSummary, format_size
from .path_detection import (
    default_backup_root,
    detect_codex_home,
    is_codex_home,
    load_or_migrate_settings,
    save_settings,
    stable_settings_path,
)
from .recycle_bin import RecycleBinClient, validate_targets
from .scanner import scan_codex_home
from .privacy import privacy_purge_history
from .storage_registry import (
    CompatibilityReport,
    CompatibilityStatus,
    StorageRegistry,
)


HISTORY_MANAGED_CATEGORIES = {
    "sessions",
    "archived_sessions",
    "logs",
    "session_index",
}


class LocalizedStringVar(tk.StringVar):
    def __init__(self, master, translator, value=""):
        self._translator = translator
        self._source_value = value
        super().__init__(master=master, value=translator(value))

    def set(self, value):
        self._source_value = value
        super().set(self._translator(value))

    def refresh(self):
        super().set(self._translator(self._source_value))


def localize_widget_tree(widget, translator):
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            child.configure(text=translator(text))
        if isinstance(child, ttk.Notebook):
            for tab_id in child.tabs():
                child.tab(tab_id, text=translator(child.tab(tab_id, "text")))
        if isinstance(child, ttk.Treeview):
            for column in child.cget("columns"):
                heading = child.heading(column, "text")
                child.heading(column, text=translator(heading))
        localize_widget_tree(child, translator)


def is_category_deletable(key: str) -> bool:
    return key not in HISTORY_MANAGED_CATEGORIES


def summarize_selection(
    items: tuple[ScanItem, ...], selected_keys: set[str]
) -> tuple[int, int, int]:
    selected = [item for item in items if item.key in selected_keys and item.exists]
    return (
        len(selected),
        sum(item.file_count for item in selected),
        sum(item.total_bytes for item in selected),
    )


def summarize_history_selection(
    records: tuple[HistoryRecord, ...], selected_ids: set[str]
) -> tuple[int, int]:
    selected = [record for record in records if record.id in selected_ids]
    related: dict[str, int] = {}
    for record in selected:
        sizes = record.related_sizes or ((record.id, record.total_bytes),)
        related.update(sizes)
    return len(selected), sum(related.values())


def calculate_space_totals(
    summary: ScanSummary,
    selected_keys: set[str],
    records: tuple[HistoryRecord, ...],
    selected_ids: set[str],
    *,
    log_diagnostics: LogDiagnostics | None = None,
) -> tuple[int, int, int]:
    reclaimable = sum(
        item.total_bytes for item in summary.items if item.key != "logs"
    )
    if log_diagnostics is not None:
        reclaimable += log_diagnostics.free_bytes
    reclaimable = min(summary.root_total_bytes, reclaimable)
    _, _, category_size = summarize_selection(
        tuple(item for item in summary.items if is_category_deletable(item.key)),
        selected_keys,
    )
    _, history_size = summarize_history_selection(records, selected_ids)
    return summary.root_total_bytes, reclaimable, category_size + history_size


def format_history_updated_at(value: str | int | float) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return "未知"
    text = str(value).replace("T", " ").replace("Z", "")[:19]
    return text or "未知"


def can_delete_history(
    selected_count: int, busy: bool, backup_ready: bool, mode: str
) -> bool:
    return bool(
        selected_count
        and not busy
        and (mode == "privacy" or backup_ready)
    )


def compatibility_status_text(report: CompatibilityReport | None) -> str:
    if report is None:
        return "存储结构：尚未检测"
    if report.status == CompatibilityStatus.SUPPORTED:
        return "存储结构：支持"
    if report.status == CompatibilityStatus.PARTIAL:
        return "存储结构：部分支持（删除前会严格校验）"
    return "存储结构：不支持（发现未知任务引用）"


class CleanupApp:
    def __init__(self, root: tk.Tk, app_dir: Path):
        self.root = root
        self.app_dir = Path(app_dir).resolve()
        self.settings_path = stable_settings_path(os.environ, Path.home())
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        self.settings = load_or_migrate_settings(
            self.settings_path,
            local / "CodexCleanupTool" / "settings.json",
            self.app_dir / "cleanup_tool_settings.json",
        )
        self.language = resolve_language(self.settings.get("language"))
        self.translator = Translator(self.language)
        self.events: queue.Queue = queue.Queue()
        self.summary: ScanSummary | None = None
        self.selected_keys: set[str] = set()
        self.history_records: tuple[HistoryRecord, ...] = ()
        self.selected_history_ids: set[str] = set()
        self.log_diagnostics: LogDiagnostics | None = None
        self.log_error: str | None = None
        self.compatibility_report: CompatibilityReport | None = None
        self.log_growth_cancel_event = None
        self.log_growth_active = False
        self.busy = False
        self.backup_ready = False

        self.path_var = tk.StringVar()
        self.history_mode_var = tk.StringVar(value="safe")
        self.language_var = tk.StringVar(value=LANGUAGE_LABELS[self.language])
        self.backup_path_var = tk.StringVar(
            value=self.settings.get("history_backup_root")
            or str(default_backup_root(Path.home()))
        )
        self.status_var = LocalizedStringVar(root, self.translator, "正在检测 Codex 数据目录...")
        self.selection_var = LocalizedStringVar(root, self.translator, "未选择任何项目")
        self.history_selection_var = LocalizedStringVar(root, self.translator, "未选择历史记录")
        self.compatibility_var = LocalizedStringVar(root, self.translator, "存储结构：尚未检测")
        self.space_var = LocalizedStringVar(root, self.translator, "总空间：未扫描 | 可清理：未扫描 | 已选择预计释放：0 B")
        self.log_size_var = LocalizedStringVar(root, self.translator, "日志数据库：未扫描")
        self.log_detail_var = LocalizedStringVar(root, self.translator, "记录数：未扫描")
        self.log_growth_var = LocalizedStringVar(root, self.translator, "增长检测：尚未检测")
        self.log_interval_var = tk.StringVar(value="10")
        self.retention_var = tk.StringVar(value="30")

        self._configure_window()
        self._build_ui()
        self.root.after(100, self._poll_events)
        self.root.after(0, self.auto_detect)
        self.root.after(200, self._initialize_backup_root)

    def _configure_window(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.title(self.translator("ChatGPT/Codex 本地历史记录清理工具"))
        self.root.geometry("1120x680")
        self.root.minsize(900, 560)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=30)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Danger.TButton", foreground="#9c1c1c")

    def _on_close(self):
        if self.busy:
            self._showwarning(
                "操作正在进行", "请等待当前操作完成后再关闭工具。",
                parent=self.root,
            )
            return
        self.root.destroy()

    def _showwarning(self, title, message, **options):
        return messagebox.showwarning(
            self._tr(title), self._tr(message), **options
        )

    def _showerror(self, title, message, **options):
        return messagebox.showerror(
            self._tr(title), self._tr(message), **options
        )

    def _showinfo(self, title, message, **options):
        return messagebox.showinfo(
            self._tr(title), self._tr(message), **options
        )

    def _askyesno(self, title, message, **options):
        return messagebox.askyesno(
            self._tr(title), self._tr(message), **options
        )

    def _askyesnocancel(self, title, message, **options):
        return messagebox.askyesnocancel(
            self._tr(title), self._tr(message), **options
        )

    def _askstring(self, title, prompt, **options):
        return simpledialog.askstring(
            self._tr(title), self._tr(prompt), **options
        )

    def _askdirectory(self, *, title, **options):
        return filedialog.askdirectory(title=self._tr(title), **options)

    def _tr(self, text):
        translator = getattr(self, "translator", None)
        return translator(text) if translator is not None else text

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="ChatGPT/Codex 本地历史记录清理工具",
            font=("Microsoft YaHei UI", 17, "bold"),
        ).pack(side="left", anchor="w")
        self.language_combo = ttk.Combobox(
            header,
            width=9,
            state="readonly",
            values=(LANGUAGE_LABELS[SIMPLIFIED_CHINESE], LANGUAGE_LABELS[ENGLISH]),
            textvariable=self.language_var,
        )
        self.language_combo.pack(side="right")
        self.language_combo.bind("<<ComboboxSelected>>", self._on_language_changed)
        ttk.Label(header, text="语言").pack(side="right", padx=(0, 8))
        ttk.Label(
            container,
            text="扫描本地 Codex 数据，支持可恢复的安全删除与不可恢复的隐私清除。",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 14))

        path_row = ttk.Frame(container)
        path_row.pack(fill="x", pady=(0, 12))
        ttk.Label(path_row, text="数据目录").pack(side="left", padx=(0, 8))
        self.path_entry = ttk.Entry(path_row, textvariable=self.path_var, state="readonly")
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.detect_button = ttk.Button(path_row, text="自动检测", command=self.auto_detect)
        self.detect_button.pack(side="left", padx=(8, 0))
        self.browse_button = ttk.Button(path_row, text="浏览", command=self.browse_path)
        self.browse_button.pack(side="left", padx=(8, 0))
        self.scan_button = ttk.Button(path_row, text="开始扫描", command=self.start_scan)
        self.scan_button.pack(side="left", padx=(8, 0))

        backup_row = ttk.Frame(container)
        backup_row.pack(fill="x", pady=(0, 12))
        ttk.Label(backup_row, text="备份目录").pack(side="left", padx=(0, 8))
        ttk.Entry(backup_row, textvariable=self.backup_path_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        self.backup_browse_button = ttk.Button(
            backup_row, text="更改", command=self.browse_backup_path
        )
        self.backup_browse_button.pack(side="left", padx=(8, 0))
        self.backup_open_button = ttk.Button(
            backup_row, text="打开", command=self.open_backup_folder
        )
        self.backup_open_button.pack(side="left", padx=(8, 0))

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self.history_tab = ttk.Frame(self.notebook, padding=8)
        self.category_tab = ttk.Frame(self.notebook, padding=8)
        self.log_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.history_tab, text="历史记录")
        self.notebook.add(self.category_tab, text="分类清理")
        self.notebook.add(self.log_tab, text="日志诊断")
        self._build_history_tab()
        self._build_category_tab()
        self._build_log_tab()

        footer = ttk.Frame(container)
        footer.pack(fill="x", pady=(12, 0))
        summary_frame = ttk.Frame(footer)
        summary_frame.pack(side="left", fill="x", expand=True)
        ttk.Label(summary_frame, textvariable=self.space_var, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(summary_frame, textvariable=self.selection_var, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(summary_frame, textvariable=self.status_var, foreground="#555555").pack(anchor="w", pady=(3, 0))

        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=110)
        self.progress.pack(side="left", padx=12)
        self.open_folder_button = ttk.Button(footer, text="打开目录", command=self.open_target_folder)
        self.open_folder_button.pack(side="left", padx=(0, 8))
        self.open_recycle_button = ttk.Button(footer, text="打开回收站", command=self.open_recycle_bin)
        self.open_recycle_button.pack(side="left", padx=(0, 8))
        self.recycle_button = ttk.Button(
            footer,
            text="移入回收站",
            style="Danger.TButton",
            command=self.confirm_recycle,
            state="disabled",
        )
        self.recycle_button.pack(side="left")
        localize_widget_tree(container, self.translator)

    def _on_language_changed(self, _event=None):
        selected = self.language_var.get()
        language = next(
            (code for code, label in LANGUAGE_LABELS.items() if label == selected),
            self.language,
        )
        if language == self.language:
            return
        if self.busy:
            self.language_var.set(LANGUAGE_LABELS[self.language])
            self._showwarning(
                "操作正在进行",
                "请等待当前操作完成后再切换语言。",
                parent=self.root,
            )
            return
        had_saved_language = "language" in self.settings
        saved_language = self.settings.get("language")
        self.settings["language"] = language
        try:
            save_settings(self.settings_path, self.settings)
        except Exception as exc:
            if had_saved_language:
                self.settings["language"] = saved_language
            else:
                self.settings.pop("language", None)
            self.language_var.set(LANGUAGE_LABELS[self.language])
            self._showerror(
                "无法保存语言设置",
                f"语言设置未更改：{exc}",
                parent=self.root,
            )
            return
        self.language = language
        self.translator.set_language(language)
        self.selected_keys.clear()
        self.selected_history_ids.clear()
        self._rebuild_localized_ui()

    def _rebuild_localized_ui(self):
        try:
            selected_tab = self.notebook.index(self.notebook.select())
        except (AttributeError, tk.TclError):
            selected_tab = 0
        self.root.title(self.translator("ChatGPT/Codex 本地历史记录清理工具"))
        for variable in (
            self.status_var,
            self.selection_var,
            self.history_selection_var,
            self.space_var,
            self.log_size_var,
            self.log_detail_var,
            self.log_growth_var,
        ):
            variable.refresh()
        for child in self.root.winfo_children():
            child.destroy()
        self._build_ui()
        self._render_summary()
        self._render_history()
        self._render_log_diagnostics()
        if selected_tab < len(self.notebook.tabs()):
            self.notebook.select(selected_tab)
        self._update_selection_summary()
        self._update_history_selection()

    def _build_log_tab(self):
        content = ttk.Frame(self.log_tab, padding=(8, 10))
        content.pack(fill="both", expand=True)
        ttk.Label(
            content,
            text="日志数据库状态",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 14))
        ttk.Label(content, textvariable=self.log_size_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=5
        )
        ttk.Label(content, textvariable=self.log_detail_var).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=5
        )
        ttk.Label(content, textvariable=self.log_growth_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=5
        )
        ttk.Label(content, text="检测周期").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.log_interval_combo = ttk.Combobox(
            content,
            width=6,
            state="readonly",
            values=("3", "10", "30"),
            textvariable=self.log_interval_var,
        )
        self.log_interval_combo.grid(row=4, column=1, sticky="w", padx=(6, 4), pady=(8, 0))
        ttk.Label(content, text="秒").grid(row=4, column=2, sticky="w", pady=(8, 0))
        ttk.Separator(content).grid(
            row=5, column=0, columnspan=4, sticky="ew", pady=18
        )
        ttk.Label(content, text="保留最近").grid(row=6, column=0, sticky="w")
        self.retention_spinbox = ttk.Spinbox(
            content,
            from_=1,
            to=365,
            width=7,
            textvariable=self.retention_var,
        )
        self.retention_spinbox.grid(row=6, column=1, sticky="w", padx=(6, 4))
        ttk.Label(content, text="天日志").grid(row=6, column=2, sticky="w")
        self.log_growth_button = ttk.Button(
            content, text="检测日志增长", command=self.start_log_growth_check
        )
        self.log_growth_button.grid(row=7, column=0, sticky="w", pady=(18, 0))
        self.log_growth_cancel_button = ttk.Button(
            content,
            text="取消检测",
            command=self.cancel_log_growth_check,
            state="disabled",
        )
        self.log_growth_cancel_button.grid(
            row=7, column=1, sticky="w", padx=(8, 0), pady=(18, 0)
        )
        self.log_optimize_button = ttk.Button(
            content,
            text="安全优化日志",
            style="Danger.TButton",
            command=self.confirm_log_optimization,
            state="disabled",
        )
        self.log_optimize_button.grid(row=7, column=2, sticky="w", padx=(8, 0), pady=(18, 0))
        content.columnconfigure(3, weight=1)

    def _build_category_tab(self):
        table_frame = ttk.Frame(self.category_tab)
        table_frame.pack(fill="both", expand=True)
        columns = ("select", "category", "files", "folders", "size", "status", "path")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "select": "选择",
            "category": "类别",
            "files": "文件数",
            "folders": "文件夹数",
            "size": "占用大小",
            "status": "状态",
            "path": "目标路径",
        }
        widths = {
            "select": 58,
            "category": 150,
            "files": 76,
            "folders": 86,
            "size": 100,
            "status": 90,
            "path": 390,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=50,
                stretch=column == "path",
                anchor="center" if column != "path" else "w",
            )
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._toggle_from_event)
        self.tree.bind("<space>", self._toggle_from_event)

    def _build_history_tab(self):
        mode_row = ttk.Frame(self.history_tab)
        mode_row.pack(fill="x", pady=(0, 8))
        ttk.Label(mode_row, text="删除模式：").pack(side="left")
        self.history_safe_radio = ttk.Radiobutton(
            mode_row,
            text="安全删除（推荐，可恢复）",
            value="safe",
            variable=self.history_mode_var,
            command=self._update_history_selection,
        )
        self.history_safe_radio.pack(side="left")
        self.history_privacy_radio = ttk.Radiobutton(
            mode_row,
            text="隐私清除（不可恢复）",
            value="privacy",
            variable=self.history_mode_var,
            command=self._update_history_selection,
        )
        self.history_privacy_radio.pack(side="left", padx=(14, 0))
        ttk.Label(
            mode_row,
            textvariable=self.compatibility_var,
            foreground="#555555",
        ).pack(side="right")

        toolbar = ttk.Frame(self.history_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(
            toolbar,
            textvariable=self.history_selection_var,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        self.history_delete_button = ttk.Button(
            toolbar,
            text="删除所选记录",
            style="Danger.TButton",
            command=self.confirm_history_delete,
            state="disabled",
        )
        self.history_delete_button.pack(side="right")
        self.history_restore_button = ttk.Button(
            toolbar, text="恢复备份", command=self.choose_history_backup
        )
        self.history_restore_button.pack(side="right", padx=(0, 8))
        self.history_backup_button = ttk.Button(
            toolbar, text="备份所选", command=self.confirm_history_backup, state="disabled"
        )
        self.history_backup_button.pack(side="right", padx=(0, 8))
        self.history_clear_button = ttk.Button(
            toolbar, text="取消选择", command=self.clear_history_selection
        )
        self.history_clear_button.pack(side="right", padx=(0, 8))
        self.history_all_button = ttk.Button(
            toolbar, text="全选", command=self.select_all_history
        )
        self.history_all_button.pack(side="right", padx=(0, 8))

        table_frame = ttk.Frame(self.history_tab)
        table_frame.pack(fill="both", expand=True)
        columns = ("select", "title", "updated", "status", "size", "id")
        self.history_tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "select": "选择",
            "title": "历史记录",
            "updated": "更新时间",
            "status": "状态",
            "size": "大小",
            "id": "任务 ID",
        }
        widths = {
            "select": 58,
            "title": 430,
            "updated": 155,
            "status": 75,
            "size": 90,
            "id": 275,
        }
        for column in columns:
            self.history_tree.heading(column, text=headings[column])
            self.history_tree.column(
                column,
                width=widths[column],
                minwidth=50,
                stretch=column == "title",
                anchor="w" if column == "title" else "center",
            )
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.history_tree.yview
        )
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.history_tree.bind("<Double-1>", self._toggle_history_from_event)
        self.history_tree.bind("<space>", self._toggle_history_from_event)

    def _set_busy(self, busy: bool, status: str | None = None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.detect_button, self.browse_button, self.scan_button,
            self.backup_browse_button, self.backup_open_button,
        ):
            button.configure(state=state)
        for radio in (self.history_safe_radio, self.history_privacy_radio):
            radio.configure(state=state)
        log_state = (
            "disabled" if busy or self.log_diagnostics is None else "normal"
        )
        self.log_growth_button.configure(state=log_state)
        self.retention_spinbox.configure(state=log_state)
        self.log_interval_combo.configure(
            state="disabled" if log_state == "disabled" else "readonly"
        )
        self.log_growth_cancel_button.configure(
            state="normal" if busy and self.log_growth_active else "disabled"
        )
        if busy:
            self.progress.start(12)
            self.recycle_button.configure(state="disabled")
            self.history_all_button.configure(state="disabled")
            self.history_clear_button.configure(state="disabled")
            self.history_delete_button.configure(state="disabled")
            self.history_backup_button.configure(state="disabled")
            self.history_restore_button.configure(state="disabled")
            self.log_optimize_button.configure(state="disabled")
        else:
            self.progress.stop()
            self.history_all_button.configure(state="normal")
            self.history_clear_button.configure(state="normal")
            self.history_restore_button.configure(
                state="normal" if self.backup_ready else "disabled"
            )
            self._update_selection_summary()
            self._update_history_selection()
            self.log_optimize_button.configure(
                state="normal" if self.log_diagnostics is not None else "disabled"
            )
        if status:
            self.status_var.set(status)

    def auto_detect(self):
        saved = self.settings.get("last_codex_home")
        detected = detect_codex_home(os.environ, Path.home(), saved)
        if detected is None:
            self.path_var.set("")
            self.status_var.set("未自动找到 .codex，请点击“浏览”选择目录。")
            self._clear_table()
            return
        self._use_path(detected)

    def browse_path(self):
        initial = self.path_var.get() or str(Path.home())
        selected = self._askdirectory(title="选择 Codex 数据目录", initialdir=initial)
        if not selected:
            return
        path = Path(selected)
        if not is_codex_home(path):
            self._showerror("目录无效", "所选目录不具备 Codex 数据目录特征。")
            return
        self._use_path(path.resolve())

    def _use_path(self, path: Path):
        self.path_var.set(str(path))
        self.settings["last_codex_home"] = str(path)
        status = "目录已就绪，请点击“开始扫描”。"
        try:
            save_settings(self.settings_path, self.settings)
        except OSError as exc:
            status = f"路径已加载，但无法保存偏好：{exc}；请点击“开始扫描”。"
        self._clear_table()
        self.status_var.set(status)
        if hasattr(self, "backup_path_var"):
            self._refresh_backup_state()

    def _initialize_backup_root(self):
        path = Path(self.backup_path_var.get())
        if path.is_dir():
            self._refresh_backup_state()
            return
        configured = bool(self.settings.get("history_backup_root"))
        prompt = (
            "已配置的历史记录备份目录不存在，可能已被移动或删除。\n\n"
            "是否现在重新创建该目录？选择“否”后可点击“更改”重新定位。"
            if configured
            else f"首次使用需要创建历史记录备份目录：\n{path}\n\n是否创建？"
        )
        if self._askyesno("准备备份目录", prompt):
            try:
                self._set_backup_root(path, create=True)
            except Exception as exc:
                self._showerror("备份目录无效", str(exc))
        else:
            self.backup_ready = False
            self.status_var.set("备份目录未就绪；历史记录删除功能已禁用。")

    def _refresh_backup_state(self):
        raw = self.backup_path_var.get()
        codex = self.path_var.get()
        if not raw or not codex:
            self.backup_ready = False
            self._update_history_selection()
            return
        try:
            ensure_backup_root(Path(raw), Path(codex))
        except BackupSafetyError:
            self.backup_ready = False
        else:
            self.backup_ready = True
        self._update_history_selection()

    def _set_backup_root(self, path: Path, *, create: bool = False):
        codex = Path(self.path_var.get()) if self.path_var.get() else Path.home() / ".codex"
        valid = ensure_backup_root(path, codex, create=create)
        self.backup_path_var.set(str(valid))
        self.settings["history_backup_root"] = str(valid)
        save_settings(self.settings_path, self.settings)
        self.backup_ready = True
        self._update_history_selection()

    def browse_backup_path(self):
        selected = self._askdirectory(
            title="选择历史记录备份目录",
            initialdir=self.backup_path_var.get() or str(Path.home() / "Documents"),
        )
        if not selected:
            return
        new = Path(selected)
        old = Path(self.backup_path_var.get())
        try:
            codex = Path(self.path_var.get())
            if old.is_dir() and old.resolve() != new.resolve() and any(
                child.is_dir() and (child / "manifest.json").is_file() for child in old.iterdir()
            ):
                choice = self._askyesnocancel(
                    "迁移已有备份",
                    "是否把旧目录中的已有备份迁移到新目录？\n"
                    "选择“是”复制并校验后删除旧备份；选择“否”仅切换目录。",
                )
                if choice is None:
                    return
                if choice:
                    new = migrate_backup_root(old, new, codex)
                    self._set_backup_root(new)
                    return
            self._set_backup_root(new, create=True)
        except Exception as exc:
            self._showerror("备份目录无效", str(exc))

    def open_backup_folder(self):
        path = Path(self.backup_path_var.get())
        if path.is_dir():
            os.startfile(path)

    def start_scan(self):
        if self.busy:
            return
        raw_path = self.path_var.get()
        if not raw_path:
            self._showwarning("没有目录", "请先自动检测或选择 .codex 目录。")
            return
        self._clear_table()
        self._set_busy(True, "正在扫描分类和历史记录...")
        threading.Thread(target=self._scan_worker, args=(Path(raw_path),), daemon=True).start()

    def _scan_worker(self, path: Path):
        try:
            summary = scan_codex_home(path)
            history = scan_history_records(path)
            all_history = scan_history_records(path, include_internal=True)
            all_ids = {record.id for record in all_history}
            registry = StorageRegistry(path)
            compatibility = registry.inspect(all_ids)
        except Exception as exc:
            self.events.put(("scan_error", str(exc)))
        else:
            try:
                diagnostics = inspect_logs(path)
                log_error = None
            except Exception as exc:
                diagnostics = None
                log_error = str(exc)
            self.events.put(
                ("scan_ok", (summary, history, diagnostics, log_error, compatibility))
            )

    def _poll_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "scan_ok":
                    (
                        self.summary,
                        self.history_records,
                        self.log_diagnostics,
                        self.log_error,
                        self.compatibility_report,
                    ) = payload
                    self.compatibility_var.set(
                        compatibility_status_text(self.compatibility_report)
                    )
                    self._render_summary()
                    self._render_history()
                    self._render_log_diagnostics()
                    self.notebook.select(self.history_tab)
                    warning_text = (
                        f"，{len(self.summary.warnings)} 个警告"
                        if self.summary.warnings
                        else ""
                    )
                    self._set_busy(
                        False,
                        f"扫描完成：找到 {len(self.history_records)} 条历史记录{warning_text}",
                    )
                elif event == "scan_error":
                    self._set_busy(False, "扫描失败")
                    self._showerror("扫描失败", payload)
                elif event == "recycle_ok":
                    self._set_busy(False, payload)
                    self._showinfo("处理完成", payload)
                    self.start_scan()
                elif event == "recycle_error":
                    self._set_busy(False, "部分或全部项目处理失败")
                    self._showerror("处理失败", payload)
                    self.start_scan()
                elif event == "history_delete_ok":
                    self._set_busy(False, payload)
                    self._showinfo("删除完成", payload)
                    self.start_scan()
                elif event == "history_delete_error":
                    self._set_busy(False, "历史记录删除失败")
                    self._showerror("删除失败", payload)
                elif event == "history_backup_ok":
                    self._set_busy(False, "任务备份完成")
                    self._showinfo("备份完成", f"备份已保存到：\n{payload}")
                elif event == "history_backup_error":
                    self._set_busy(False, "任务备份失败")
                    self._showerror("备份失败", payload)
                elif event == "history_restore_ok":
                    self._set_busy(False, "任务恢复完成")
                    self._showinfo("恢复完成", payload)
                    self.start_scan()
                elif event == "history_restore_error":
                    self._set_busy(False, "任务恢复失败")
                    self._showerror("恢复失败", payload)
                elif event == "log_growth_ok":
                    self.log_growth_active = False
                    self.log_growth_cancel_event = None
                    self._set_busy(False, "日志增长检测完成")
                    self.log_growth_var.set(payload)
                    self.notebook.select(self.log_tab)
                elif event == "log_growth_error":
                    self.log_growth_active = False
                    self.log_growth_cancel_event = None
                    self._set_busy(False, "日志增长检测失败")
                    self._showerror("检测失败", payload)
                elif event == "log_growth_cancelled":
                    self.log_growth_active = False
                    self.log_growth_cancel_event = None
                    self._set_busy(False, "日志增长检测已取消")
                    self.log_growth_var.set("增长检测：已取消")
                elif event == "log_optimize_ok":
                    self._set_busy(False, payload)
                    self._showinfo("日志优化完成", payload)
                    self.start_scan()
                elif event == "log_optimize_error":
                    self._set_busy(False, "日志优化失败")
                    self._showerror("日志优化失败", payload)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _clear_table(self):
        self.summary = None
        self.selected_keys.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.history_records = ()
        self.log_diagnostics = None
        self.log_error = None
        self.compatibility_report = None
        self.compatibility_var.set("存储结构：尚未检测")
        self.selected_history_ids.clear()
        for row in self.history_tree.get_children():
            self.history_tree.delete(row)
        self._update_selection_summary()
        self._update_history_selection()
        self._render_log_diagnostics()
        self._update_space_summary()

    def _render_summary(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        if self.summary is None:
            return
        for item in self.summary.items:
            paths = "; ".join(str(path) for path in item.paths) if item.paths else self._tr("未找到")
            if item.key == "logs":
                status = "请在日志诊断中优化"
            elif item.key in HISTORY_MANAGED_CATEGORIES:
                status = "请在历史记录中删除"
            else:
                status = "可选择" if item.exists else "未找到"
            self.tree.insert(
                "",
                "end",
                iid=item.key,
                values=(
                    "[ ]",
                    self._tr(item.label),
                    item.file_count,
                    item.folder_count,
                    format_size(item.total_bytes),
                    self._tr(status),
                    paths,
                ),
                tags=(
                    "managed"
                    if item.key in HISTORY_MANAGED_CATEGORIES
                    else "available" if item.exists else "missing"
                ,),
            )
        self.tree.tag_configure("missing", foreground="#888888")
        self.tree.tag_configure("managed", foreground="#777777")
        self._update_selection_summary()

    def _render_log_diagnostics(self):
        diagnostics = self.log_diagnostics
        if diagnostics is None:
            self.log_size_var.set(
                f"日志诊断失败：{self.log_error}"
                if self.log_error
                else "日志数据库：未找到或尚未扫描"
            )
            self.log_detail_var.set("记录数：未扫描")
            self.log_growth_button.configure(state="disabled")
            self.log_growth_cancel_button.configure(state="disabled")
            self.log_interval_combo.configure(state="disabled")
            self.retention_spinbox.configure(state="disabled")
            self.log_optimize_button.configure(state="disabled")
            return
        self.log_size_var.set(
            "日志数据库："
            f"{format_size(diagnostics.total_bytes)} "
            f"(主库 {format_size(diagnostics.database_bytes)}，"
            f"WAL {format_size(diagnostics.wal_bytes)})；"
            f"库内可回收约 {format_size(diagnostics.free_bytes)}"
        )
        self.log_detail_var.set(
            f"记录数：{diagnostics.row_count}；TRACE：{diagnostics.trace_count} "
            f"({diagnostics.trace_ratio:.1%})"
        )
        self.log_optimize_button.configure(
            state="normal" if not self.busy else "disabled"
        )

    def _render_history(self):
        for row in self.history_tree.get_children():
            self.history_tree.delete(row)
        for record in self.history_records:
            self.history_tree.insert(
                "",
                "end",
                iid=record.id,
                values=(
                    "[ ]",
                    record.title,
                    self._tr(format_history_updated_at(record.updated_at)),
                    self._tr(
                        "会话文件缺失"
                        if record.missing_rollout
                        else "已归档" if record.archived else "当前"
                    ),
                    format_size(record.total_bytes),
                    record.id,
                ),
            )
        self._update_history_selection()

    def _toggle_history_from_event(self, _event=None):
        if self.busy or not self.history_records:
            return "break"
        row = self.history_tree.focus()
        if not row:
            selection = self.history_tree.selection()
            row = selection[0] if selection else ""
        if not row:
            return "break"
        if row in self.selected_history_ids:
            self.selected_history_ids.remove(row)
            mark = "[ ]"
        else:
            self.selected_history_ids.add(row)
            mark = "[x]"
        values = list(self.history_tree.item(row, "values"))
        values[0] = mark
        self.history_tree.item(row, values=values)
        self._update_history_selection()
        return "break"

    def select_all_history(self):
        if self.busy:
            return
        self.selected_history_ids = {record.id for record in self.history_records}
        for row in self.history_tree.get_children():
            values = list(self.history_tree.item(row, "values"))
            values[0] = "[x]"
            self.history_tree.item(row, values=values)
        self._update_history_selection()

    def clear_history_selection(self):
        if self.busy:
            return
        self.selected_history_ids.clear()
        for row in self.history_tree.get_children():
            values = list(self.history_tree.item(row, "values"))
            values[0] = "[ ]"
            self.history_tree.item(row, values=values)
        self._update_history_selection()

    def _update_history_selection(self):
        count, size = summarize_history_selection(
            self.history_records, self.selected_history_ids
        )
        if count:
            self.history_selection_var.set(
                f"已选择 {count} 条历史记录，约 {format_size(size)}"
            )
        else:
            self.history_selection_var.set(
                f"共 {len(self.history_records)} 条历史记录，未选择"
                if self.history_records
                else "未选择历史记录"
            )
        self.history_delete_button.configure(
            state="normal"
            if can_delete_history(
                count,
                self.busy,
                self.backup_ready,
                self.history_mode_var.get(),
            )
            else "disabled"
        )
        self.history_backup_button.configure(
            state="normal" if count and not self.busy and self.backup_ready else "disabled"
        )
        self._update_space_summary()

    def confirm_history_delete(self):
        if not self.selected_history_ids or self.busy:
            return
        selected = [
            record
            for record in self.history_records
            if record.id in self.selected_history_ids
        ]
        count, size = summarize_history_selection(
            self.history_records, self.selected_history_ids
        )
        titles = "\n".join(f"- {record.title}" for record in selected[:8])
        if len(selected) > 8:
            titles += f"\n- 另有 {len(selected) - 8} 条"
        mode = self.history_mode_var.get()
        if mode == "privacy":
            message = (
                f"将永久清除 {count} 条本地历史记录，文件约 {format_size(size)}。\n\n"
                f"{titles}\n\n"
                "工具不会创建备份，会永久删除已知数据库、全局状态、任务索引、"
                "锁文件、关联日志和会话文件中的任务引用，并在完成后验证。\n"
                "必须完全退出 Codex。此操作不可恢复，是否继续？"
            )
            if not self._askyesno("确认隐私清除", message, icon="warning"):
                return
            phrase = "永久清除" if self.language == SIMPLIFIED_CHINESE else "PERMANENT DELETE"
            entered = self._askstring(
                "再次确认隐私清除",
                f"请输入“{phrase}”以确认不可恢复操作：",
                parent=self.root,
            )
            if entered != phrase:
                self._showwarning("确认内容不匹配", "未执行隐私清除。")
                return
        else:
            message = (
                f"将删除 {count} 条本地历史记录，文件约 {format_size(size)}。\n\n"
                f"{titles}\n\n"
                "删除前会创建永久备份；会话文件随后移入 Windows 回收站，"
                "并同步移除数据库关系、本地任务索引及相同任务 ID 的关联日志。\n"
                "删除前必须完全退出 Codex 桌面程序。是否继续？"
            )
            if not self._askyesno("确认删除历史记录", message, icon="warning"):
                return
        selected_ids = set(self.selected_history_ids)
        self._set_busy(
            True,
            "正在永久清除并验证所选历史记录..."
            if mode == "privacy"
            else "正在校验并删除所选历史记录...",
        )
        threading.Thread(
            target=self._history_delete_worker,
            args=(Path(self.path_var.get()), selected_ids, mode),
            daemon=False,
        ).start()

    def _history_delete_worker(
        self, root: Path, selected_ids: set[str], mode: str = "safe"
    ):
        try:
            if mode == "privacy":
                result = privacy_purge_history(root, selected_ids)
            else:
                result = delete_history_records(
                    root, selected_ids, backup_root=Path(self.backup_path_var.get())
                )
        except Exception as exc:
            self.events.put(("history_delete_error", str(exc)))
            return
        self.selected_history_ids.clear()
        if mode == "privacy":
            message = (
                f"已永久清除 {len(result.deleted_ids)} 条本地历史记录，"
                f"共移除 {result.deleted_references} 个已知本地引用。\n"
                "本次操作不会创建备份。"
            )
        else:
            message = (
                f"已删除 {len(result.deleted_ids)} 条本地历史记录及 "
                f"{result.deleted_log_rows} 条关联日志、"
                f"{result.deleted_auxiliary_references} 个辅助引用。\n"
                f"永久备份：{result.backup_path}"
            )
        self.events.put(
            (
                "history_delete_ok",
                message,
            )
        )

    def confirm_history_backup(self):
        if self.busy or not self.selected_history_ids or not self.backup_ready:
            return
        selected_ids = set(self.selected_history_ids)
        self._set_busy(True, "正在创建并校验任务备份...")
        threading.Thread(
            target=self._history_backup_worker,
            args=(Path(self.path_var.get()), selected_ids),
            daemon=False,
        ).start()

    def _history_backup_worker(self, root: Path, selected_ids: set[str]):
        try:
            result = create_history_backup(
                root, selected_ids, Path(self.backup_path_var.get())
            )
        except Exception as exc:
            self.events.put(("history_backup_error", str(exc)))
            return
        self.events.put(("history_backup_ok", str(result.path)))

    def choose_history_backup(self):
        if self.busy or not self.backup_ready:
            return
        selected = self._askdirectory(
            title="选择要恢复的任务备份文件夹",
            initialdir=self.backup_path_var.get(),
        )
        if not selected:
            return
        if not self._askyesno(
            "确认恢复任务",
            "恢复前必须完全退出 Codex。工具会校验备份，并在冲突时拒绝覆盖。是否继续？",
            icon="warning",
        ):
            return
        self._set_busy(True, "正在校验并恢复任务备份...")
        threading.Thread(
            target=self._history_restore_worker,
            args=(Path(selected), Path(self.path_var.get())),
            daemon=False,
        ).start()

    def _history_restore_worker(self, backup: Path, root: Path):
        try:
            result = restore_history_backup(backup, root)
        except Exception as exc:
            self.events.put(("history_restore_error", str(exc)))
            return
        self.events.put(
            (
                "history_restore_ok",
                f"已恢复 {len(result.restored_ids)} 条任务记录及 "
                f"{result.restored_log_rows} 条关联日志。",
            )
        )

    def _toggle_from_event(self, _event=None):
        if self.busy or self.summary is None:
            return "break"
        row = self.tree.focus()
        if not row:
            selection = self.tree.selection()
            row = selection[0] if selection else ""
        if not row:
            return "break"
        try:
            item = self.summary.by_key(row)
        except KeyError:
            return "break"
        if not item.exists or not is_category_deletable(item.key):
            return "break"
        if row in self.selected_keys:
            self.selected_keys.remove(row)
            mark = "[ ]"
        else:
            self.selected_keys.add(row)
            mark = "[x]"
        values = list(self.tree.item(row, "values"))
        values[0] = mark
        self.tree.item(row, values=values)
        self._update_selection_summary()
        return "break"

    def _update_selection_summary(self):
        items = (
            tuple(
                item
                for item in self.summary.items
                if is_category_deletable(item.key)
            )
            if self.summary
            else ()
        )
        categories, files, size = summarize_selection(items, self.selected_keys)
        if categories:
            self.selection_var.set(
                f"已选择 {categories} 类，{files} 个文件，约 {format_size(size)}"
            )
        else:
            self.selection_var.set("未选择任何项目")
        self.recycle_button.configure(
            state="normal" if categories and not self.busy else "disabled"
        )
        self._update_space_summary()

    def _update_space_summary(self):
        if self.summary is None:
            self.space_var.set(
                "总空间：未扫描 | 可清理：未扫描 | 已选择预计释放：0 B"
            )
            return
        total, reclaimable, selected = calculate_space_totals(
            self.summary,
            self.selected_keys,
            self.history_records,
            self.selected_history_ids,
            log_diagnostics=self.log_diagnostics,
        )
        self.space_var.set(
            f"总空间：{format_size(total)} | 可清理：{format_size(reclaimable)} | "
            f"已选择预计释放：{format_size(selected)}"
        )

    def start_log_growth_check(self):
        if self.busy or self.log_diagnostics is None:
            return
        interval_seconds = int(self.log_interval_var.get())
        self.log_growth_cancel_event = threading.Event()
        self.log_growth_active = True
        self._set_busy(True, f"正在进行 {interval_seconds} 秒日志增长检测...")
        self._update_log_growth_countdown(interval_seconds)
        threading.Thread(
            target=self._log_growth_worker,
            args=(
                Path(self.path_var.get()),
                interval_seconds,
                self.log_growth_cancel_event,
            ),
            daemon=True,
        ).start()

    def _update_log_growth_countdown(self, remaining: int):
        if not self.log_growth_active:
            return
        if remaining <= 0:
            self.log_growth_var.set("增长检测：正在计算结果...")
            return
        self.log_growth_var.set(f"增长检测：正在采样，剩余 {remaining} 秒")
        self.root.after(1000, self._update_log_growth_countdown, remaining - 1)

    def cancel_log_growth_check(self):
        if self.log_growth_cancel_event is None:
            return
        self.log_growth_cancel_event.set()
        self.log_growth_cancel_button.configure(state="disabled")
        self.log_growth_var.set("增长检测：正在取消...")

    def _log_growth_worker(self, root: Path, interval_seconds: int, cancel_event):
        try:
            growth = sample_log_growth(
                root,
                interval_seconds=interval_seconds,
                cancel_event=cancel_event,
            )
        except LogGrowthCancelled:
            self.events.put(("log_growth_cancelled", None))
            return
        except Exception as exc:
            self.events.put(("log_growth_error", str(exc)))
            return
        state_text = {
            "idle": "未检测到增长",
            "active": "检测到写入",
            "high": "高频增长",
        }[classify_log_growth(growth)]
        text = (
            f"增长检测：{state_text} | 新增 {growth.rows_per_minute:.0f} 条/分钟，"
            f"TRACE {growth.trace_rows_per_minute:.0f} 条/分钟，"
            f"文件增长 {format_size(int(growth.bytes_per_minute))}/分钟"
        )
        self.events.put(("log_growth_ok", text))

    def confirm_log_optimization(self):
        if self.busy or self.log_diagnostics is None:
            return
        try:
            retention_days = int(self.retention_var.get())
        except ValueError:
            self._showwarning("参数无效", "日志保留天数必须是整数。")
            return
        if not 1 <= retention_days <= 365:
            self._showwarning("参数无效", "日志保留天数必须在 1 到 365 之间。")
            return
        message = (
            f"将保留最近 {retention_days} 天日志，删除更早记录并压缩数据库。\n\n"
            "开始前必须完全退出 Codex 桌面程序。工具会先创建临时备份，"
            "执行完整性检查；失败时自动恢复。是否继续？"
        )
        if not self._askyesno("确认安全优化日志", message, icon="warning"):
            return
        self._set_busy(True, "正在备份、清理并校验日志数据库...")
        threading.Thread(
            target=self._log_optimize_worker,
            args=(Path(self.path_var.get()), retention_days),
            daemon=False,
        ).start()

    def _log_optimize_worker(self, root: Path, retention_days: int):
        try:
            result = optimize_logs(root, retention_days=retention_days)
        except Exception as exc:
            self.events.put(("log_optimize_error", str(exc)))
            return
        self.events.put(
            (
                "log_optimize_ok",
                f"已删除 {result.deleted_rows} 条过期日志，实际释放 "
                f"{format_size(result.released_bytes)}。",
            )
        )

    def confirm_recycle(self):
        if self.summary is None or not self.selected_keys or self.busy:
            return
        selected = [
            item
            for item in self.summary.items
            if item.key in self.selected_keys
            and item.exists
            and is_category_deletable(item.key)
        ]
        categories, files, size = summarize_selection(
            tuple(item for item in self.summary.items if is_category_deletable(item.key)),
            self.selected_keys,
        )
        path_lines = [str(path) for item in selected for path in item.paths]
        message = (
            f"将把 {categories} 类记录移入 Windows 回收站。\n"
            f"包含 {files} 个文件，约 {format_size(size)}。\n\n"
            + "\n".join(path_lines)
            + "\n\n可以从 Windows 回收站恢复。是否继续？"
        )
        if not self._askyesno("确认移入回收站", message, icon="warning"):
            return
        self._set_busy(True, "正在进行安全校验并移入回收站...")
        threading.Thread(
            target=self._recycle_worker,
            args=(self.summary.root, tuple(selected), size),
            daemon=True,
        ).start()

    def _recycle_worker(self, root: Path, items: tuple[ScanItem, ...], size: int):
        try:
            validated: list[Path] = []
            for item in items:
                validated.extend(validate_targets(root, item.key, item.paths))
            result = RecycleBinClient().recycle(tuple(validated))
            if result.failed:
                details = "\n".join(f"{path}：{reason}" for path, reason in result.failed)
                self.events.put(("recycle_error", details))
                return
            self.selected_keys.clear()
            self.events.put(
                (
                    "recycle_ok",
                    f"已将 {len(result.succeeded)} 个目标移入回收站，预计释放 {format_size(size)}。",
                )
            )
        except Exception as exc:
            self.events.put(("recycle_error", str(exc)))

    def open_target_folder(self):
        path = self.path_var.get()
        if path and Path(path).is_dir():
            os.startfile(path)

    def open_recycle_bin(self):
        try:
            subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
        except OSError as exc:
            self._showerror("无法打开回收站", str(exc))
