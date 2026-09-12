#!/usr/bin/env python3
"""hermes-lean 圖形介面（小白版）。

只有一顆大按鈕：更新 Hermes 之後按一下，就會把精簡描述寫回去。
程式會自己判斷「現在需不需要按」，用顏色告訴你。

進階設定（安裝位置、設定檔、skill 白名單、索引模式）收在「進階設定」裡，
平常不需要打開。

本介面只是前端：每個動作都是呼叫同資料夾的 apply_compact.py，行為與命令列完全一致。
不需要任何第三方套件。

執行：python compact_ui.py   （或直接雙擊 compact_ui.bat / compact_ui.sh）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import apply_compact as hc  # 重用路徑偵測與合併邏輯
except Exception:  # pragma: no cover - 介面仍可用命令列參數運作
    hc = None

SCRIPT = HERE / "apply_compact.py"
STATE_FILE = HERE / "ui_state.json"
DEFAULT_BASE = HERE / "overrides" / "compact-overrides.json"
DEFAULT_PRIVATE = HERE / "overrides.d" / "90-private.json"
EXAMPLE_BASE = HERE / "compact-overrides.example.json"

CAPTURE_WARNING = (
    "這只會匯出 hermes-lean 支援的欄位，不代表完整 Hermes 設定。\n"
    "內容可能包含個人化提示詞或其他私密資料，請勿直接分享。\n\n"
    "This exports only fields supported by hermes-lean, not the complete Hermes config.\n"
    "It may contain personalized or private text; do not share it without review."
)

# 橫幅最長／最短的說明文字。用來預先量出固定的橫幅高度，讓主視窗尺寸永遠不變
# （否則三行訊息會把大按鈕與「進階設定」推出視窗下緣）。
LONGEST_BANNER_DETAIL = (
    "有 99 個檔案目前是原始版本（通常是剛更新完 Hermes）。\n"
    "99 file(s) are back to upstream (usually right after an update).\n"
    "按上面的按鈕就能修好。 / Press the button above to fix it."
)
SHORT_BANNER_DETAIL = (
    "精簡描述已經套用好了，目前不需要做任何事。\n"
    "Compact descriptions are applied. Nothing to do."
)

# 主按鈕用的字型（比預設大，方便一眼找到）
FONT_FAMILY = "Microsoft JhengHei UI"
BIG = (FONT_FAMILY, 14, "bold")
TITLE = (FONT_FAMILY, 12, "bold")
SMALL = (FONT_FAMILY, 10)

# 狀態卡的四種樣子：底色、文字色、圖示
STATES = {
    "ok":    ("#e6f4ea", "#137333", "✅"),
    "todo":  ("#fef7e0", "#b06000", "⚠️"),
    "error": ("#fce8e6", "#b00020", "❌"),
    "busy":  ("#e8f0fe", "#1a73e8", "⏳"),
}


def default_base_config() -> Path:
    """回傳預設基礎設定檔。

    全新使用者還沒有 overrides/compact-overrides.json，這時改用範例檔，
    讓程式開箱即可運作（範例檔是可公開的通用文案）。
    """
    return DEFAULT_BASE if DEFAULT_BASE.is_file() else EXAMPLE_BASE


def existing_or_none(path_str: str) -> str:
    """若路徑存在就原樣回傳，否則回傳空字串（避免沿用已失效的舊路徑）。"""
    try:
        return path_str if path_str and Path(path_str).is_file() else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 高 DPI：沒有這一段，Windows 會把視窗點陣放大 -> 字變模糊
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> None:
    """宣告 per-monitor v2 DPI 感知，讓 Tk 直接以實體像素繪製（字才清晰）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:  # Windows 10 1703+
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:  # Windows 8.1+
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()  # 最後的後備
    except Exception:
        pass


def work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """回傳可用桌面區域 (left, top, right, bottom)，已排除工作列。

    Tk 的 winfo_screenheight() 是「整個螢幕」，不含工作列資訊；
    若直接拿它定位，視窗底邊（含標題列）會壓到工作列底下，
    使用者就看不到底部的按鈕。改用 Windows 的 SPI_GETWORKAREA。
    """
    try:
        if os.name == "nt":
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            r = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                if r.right > r.left and r.bottom > r.top:
                    return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def ui_scale(root: tk.Misc) -> float:
    """回傳相對於 96 DPI 的縮放倍率（150% 縮放 -> 1.5）。"""
    try:
        return max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


def apply_scaling(root: tk.Tk) -> None:
    """讓 Tk 的點（point）字級對應到真實 DPI。"""
    try:
        root.tk.call("tk", "scaling", float(root.winfo_fpixels("1i")) / 72.0)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 共用小工具
# --------------------------------------------------------------------------- #
def detect_hermes_home() -> Path:
    if hc is not None:
        return hc.default_hermes_home()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "hermes"
    return Path.home() / ".hermes"


def candidate_roots() -> list[Path]:
    home = detect_hermes_home()
    cands = [home / "hermes-agent", home]
    # 幾種常見的替代安裝位置。
    cands += [Path.home() / ".hermes" / "hermes-agent", Path("/opt/hermes/hermes-agent")]
    return cands


def looks_like_checkout(path: Path) -> bool:
    return (path / "model_tools.py").is_file() and (path / "agent" / "prompt_builder.py").is_file()


def autodetect_root() -> Path | None:
    for cand in candidate_roots():
        if looks_like_checkout(cand):
            return cand
    return None


def find_python(root: Path | None) -> str:
    """Run the frontend with the already-trusted interpreter, never a selected checkout's executable."""
    return sys.executable


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def read_json(path: Path, *, strict: bool = False) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"),
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant: {value}")))
        if not isinstance(data, dict):
            raise ValueError(f"JSON root must be an object: {path}")
        return data
    except Exception as exc:
        if strict:
            raise ValueError(f"Cannot read valid JSON config {path}: {exc}") from exc
        return {}


def safe_config_path(path: Path) -> bool:
    """Only regular, non-link .json files may be handed to the system file opener."""
    try:
        return path.suffix.lower() == ".json" and path.is_file() and not path.is_symlink()
    except OSError:
        return False


def editor_command(path: Path) -> list[str]:
    if not safe_config_path(path):
        raise ValueError(f"Only an existing regular JSON config may be opened: {path}")
    resolved = path.resolve(strict=True)
    if resolved.suffix.lower() != ".json":
        raise ValueError(f"Resolved config is not JSON: {resolved}")
    if os.name == "nt":
        return ["notepad.exe", str(resolved)]
    if sys.platform == "darwin":
        return ["open", "-t", str(resolved)]
    return ["xdg-open", str(resolved)]


def save_private_json(data: dict) -> None:
    """Validate and atomically replace the one UI-managed private config."""
    target = DEFAULT_PRIVATE
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise ValueError(f"Refusing unsafe private config target: {target}")
    if hc is not None:
        hc.validate_config(data, source=str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, raw = tempfile.mkstemp(prefix=target.name + ".hc-tmp-", dir=str(target.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 介面
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("hermes-lean — 一鍵套用精簡描述 / One-click Apply")
        self.s = ui_scale(root)

        state = load_state()
        self.hermes_root = tk.StringVar(value=state.get("hermes_root") or str(autodetect_root() or ""))
        saved_base = existing_or_none(state.get("base_config") or "")
        self.base_config = tk.StringVar(value=saved_base or str(default_base_config()))
        self.extra_configs: list[str] = [
            p for p in (state.get("extra_configs") or []) if existing_or_none(p)
        ]
        self._busy = False
        self._proc: subprocess.Popen[str] | None = None
        self._current_action = ""
        self._adv_shown = False          # 進階子視窗是否開著
        self._show_status_bar = False

        self._build()
        self._lock_dynamic_heights()   # 橫幅／狀態列固定高度，之後換文字不再改變版面
        self._finalize_geometry()
        # 開窗後自動檢查一次狀態，讓使用者一打開就知道要不要按。
        self.root.after(250, self.check_status)

    def _lock_dynamic_heights(self) -> None:
        """固定橫幅與狀態列的高度，讓主視窗尺寸永遠不變。

        先量出「最長的訊息」需要多高再把橫幅高度鎖死，因此任何狀態文字都不會
        改動版面，大按鈕與「進階設定」永遠留在視窗內。
        """
        try:
            banner = self.banner
            need = 0
            for detail in (LONGEST_BANNER_DETAIL, SHORT_BANNER_DETAIL):
                self.banner_detail.configure(text=detail)
                self.root.update_idletasks()
                need = max(need, banner.winfo_reqheight())
            self.banner.configure(height=need)
            banner.pack_propagate(False)
            self._banner_h = need

            bar = self.status_frame
            self.status.configure(text="狀態 / status")
            bar.pack(fill="x", pady=(10, 0))
            self.root.update_idletasks()
            self._status_bar_h = bar.winfo_reqheight()
            bar.pack_forget()
        except Exception:
            self._banner_h = 0
            self._status_bar_h = 0

    def _finalize_geometry(self) -> None:
        """依「內容實際需要的尺寸」決定視窗大小。

        高 DPI（例如 200% 縮放）時文字與控件都會放大，而 winfo_reqwidth /
        winfo_reqheight 在縮放後會低估（Text / Listbox 的行高會變大），
        直接用會把右側按鈕與底部輸出框切掉。因此改為實際走訪控件樹，量出
        內容真正的邊界，再夾在螢幕範圍內。
        """
        root = self.root
        root.update_idletasks()
        s = getattr(self, "s", 1.0)
        wl, wt, wr, wb = work_area(root)
        sw, sh = wr - wl, wb - wt
        need_w, need_h = self._content_size()
        # 貼合內容：地板值只防止視窗過小，不可高於內容本身，否則收合時會留一大塊空白。
        w = min(max(need_w + 48, int(460 * s)), max(600, sw - 60))
        h = min(max(need_h + 64, int(180 * s)), max(360, sh - 90))
        root.geometry(f"{w}x{h}")
        # 最小尺寸等於貼合後的大小，內容永遠不會被裁掉。
        root.minsize(w, h)

    def _content_size(self, target: tk.Misc | None = None) -> tuple[int, int]:
        """回傳所有「已排版」子控件在該視窗座標系中的最大右緣與下緣。

        - 未顯示（pack_forget / withdraw）的控件不算，否則會把視窗撐大。
        - 一定要跳過 Toplevel：子視窗在 Tk 樹中是 root 的子節點，
          它的座標會讓主視窗被誤判成需要超大尺寸。
        """
        best = [0, 0]
        root_widget = target if target is not None else self.root

        def walk(wdg: tk.Misc, ox: int, oy: int) -> None:
            for c in wdg.winfo_children():
                if isinstance(c, tk.Toplevel):  # 子視窗不參與本視窗尺寸計算
                    continue
                if not c.winfo_manager():       # 未排版 -> 跳過
                    continue
                cx, cy = ox + c.winfo_x(), oy + c.winfo_y()
                best[0] = max(best[0], cx + c.winfo_reqwidth())
                best[1] = max(best[1], cy + c.winfo_reqheight())
                walk(c, cx, cy)

        walk(root_widget, 0, 0)
        return best[0], best[1]

    # -- 版面 --------------------------------------------------------------- #
    def _build(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # ── 狀態卡：一眼看出現在要不要按 ──────────────────────────────────
        self.banner = tk.Frame(outer, bd=1, relief="solid")
        self.banner.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(self.banner)
        inner.pack(fill="x", padx=14, pady=12)
        self.banner_icon = tk.Label(inner, text="⏳", font=(FONT_FAMILY, 20))
        self.banner_icon.pack(side="left", padx=(0, 12))
        text_col = tk.Frame(inner)
        text_col.pack(side="left", fill="x", expand=True)
        self.banner_title = tk.Label(text_col, text="檢查中… / Checking…", font=TITLE, anchor="w", justify="left")
        self.banner_title.pack(fill="x")
        self.banner_detail = tk.Label(text_col, text="", font=SMALL, anchor="w", justify="left", wraplength=560)
        self.banner_detail.pack(fill="x")
        self._paint_banner("busy")

        # ── 唯一的一顆大按鈕 ───────────────────────────────────────────────
        self.btn_apply = tk.Button(
            outer, text="★  一鍵套用  /  Apply", font=BIG,
            bg="#1a73e8", fg="white", activebackground="#1558b0", activeforeground="white",
            disabledforeground="#dce9ff",   # 停用時仍維持可讀，不要變成灰字
            relief="raised", bd=2, cursor="hand2", command=lambda: self.run_action("apply"),
        )
        self.btn_apply.pack(fill="x", ipady=14)
        ttk.Label(
            outer,
            text="按一下就好。會自動備份原檔（.bak），完成後要完整重開 Hermes 才生效。\n"
                 "Just press it once. Originals are backed up (.bak); fully quit and relaunch Hermes to take effect.",
            font=SMALL, foreground="#555", justify="center",
        ).pack(pady=(8, 2))

        # ── 進階設定開關 ───────────────────────────────────────────────────
        self.btn_adv = ttk.Button(outer, text="", command=self._toggle_advanced)
        self.btn_adv.pack(anchor="w", pady=(10, 0))
        self._sync_adv_label()

        # ── 進階設定：獨立子視窗（延後到第一次點開才建立，開場不會閃）────
        self.adv_win: tk.Toplevel | None = None
        self.adv: ttk.Frame | None = None
        self._canvas: tk.Canvas | None = None

        # ── 狀態列（只有真的在跑指令時才顯示，平常不佔空間）───────────────
        self.status_frame = ttk.Frame(outer)
        self.status = ttk.Label(self.status_frame, text="", anchor="w", foreground="#555")
        self.status.pack(side="left", fill="x", expand=True)

    def _build_advanced(self, adv: ttk.Frame) -> None:
        pad = {"padx": 6, "pady": 4}

        # 安裝位置
        box = ttk.LabelFrame(adv, text="Hermes 安裝位置 / Install location")
        box.pack(fill="x", **pad)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Entry(row, textvariable=self.hermes_root).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="自動偵測 / Auto", command=self.on_autodetect).pack(side="left", padx=4)
        ttk.Button(row, text="瀏覽… / Browse…", command=self.on_browse_root).pack(side="left")
        self.root_status = ttk.Label(box, text="", foreground="#555")
        self.root_status.pack(anchor="w", padx=10, pady=(0, 6))
        self.hermes_root.trace_add("write", lambda *_: self.refresh_root_status())
        self.refresh_root_status()

        # 設定檔
        cfgbox = ttk.LabelFrame(adv, text="設定檔 / Config files（下面的覆蓋上面的 / later wins）")
        cfgbox.pack(fill="x", **pad)
        r = ttk.Frame(cfgbox)
        r.pack(fill="x", padx=8, pady=6)
        ttk.Label(r, text="基礎 / Base：").pack(side="left")
        ttk.Entry(r, textvariable=self.base_config).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r, text="瀏覽… / Browse…", command=self.on_browse_base).pack(side="left")

        r2 = ttk.Frame(cfgbox)
        r2.pack(fill="x", padx=8, pady=(0, 6))
        left = ttk.Frame(r2)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="額外設定（唯讀）/ Extra configs (read-only)：").pack(anchor="w")
        self.cfg_list = tk.Listbox(left, height=3)
        self.cfg_list.pack(fill="both", expand=True)
        btns = ttk.Frame(r2)
        btns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(btns, text="加入檔案… / Add…", command=self.on_add_config).pack(fill="x", pady=2)
        ttk.Button(btns, text="建立私有檔 / Create private", command=self.on_new_private).pack(fill="x", pady=2)
        ttk.Button(btns, text="開啟 / Open", command=self.on_open_config).pack(fill="x", pady=2)
        ttk.Button(btns, text="移除 / Remove", command=self.on_remove_config).pack(fill="x", pady=2)
        self._refresh_cfg_list()

        # Skill 白名單
        skbox = ttk.LabelFrame(adv, text="要保留說明的 skill / Keep-description skills（其餘只顯示名稱 / rest = names only）")
        skbox.pack(fill="x", **pad)
        sr = ttk.Frame(skbox)
        sr.pack(fill="x", padx=8, pady=6)
        self.skill_list = tk.Listbox(sr, height=4)
        self.skill_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Frame(sr)
        sb.pack(side="left", fill="y", padx=(6, 0))
        ttk.Label(sb, text="skill 名稱 / name：").pack(anchor="w")
        self.skill_entry = ttk.Entry(sb, width=24)
        self.skill_entry.pack(fill="x", pady=2)
        ttk.Button(sb, text="加入 / Add", command=self.on_add_skill).pack(fill="x", pady=2)
        ttk.Button(sb, text="移除 / Remove", command=self.on_remove_skill).pack(fill="x", pady=2)
        self.skill_hint = ttk.Label(skbox, text="", foreground="#555")
        self.skill_hint.pack(anchor="w", padx=10, pady=(6, 2))

        gr = ttk.Frame(skbox)
        gr.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(gr, text="精簡範圍 / Index mode：").pack(side="left")
        self.gate = tk.StringVar(value=self._load_gate())
        ttk.Radiobutton(gr, text="全部介面 / everywhere", value="focus",
                        variable=self.gate, command=self._save_gate).pack(side="left", padx=6)
        ttk.Radiobutton(gr, text="只在程式工作區 / coding only", value="focus_and_coding",
                        variable=self.gate, command=self._save_gate).pack(side="left")
        self._refresh_skills()

        # 其他動作
        act = ttk.Frame(adv)
        act.pack(fill="x", **pad)
        ttk.Label(act, text="其他 / More：").pack(side="left")
        self.btn_check = ttk.Button(act, text="只檢查 / Check", command=lambda: self.run_action("check"))
        self.btn_restore = ttk.Button(act, text="還原 / Restore", command=lambda: self.run_action("restore"))
        self.btn_capture = ttk.Button(act, text="擷取目前值 / Capture…", command=self.on_capture)
        for b in (self.btn_check, self.btn_restore, self.btn_capture):
            b.pack(side="left", padx=4)

        # 輸出訊息
        logbox = ttk.LabelFrame(adv, text="輸出訊息 / Output")
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, height=8, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb2 = ttk.Scrollbar(logbox, command=self.log.yview)
        sb2.pack(side="right", fill="y", pady=6, padx=(0, 8))
        self.log.configure(yscrollcommand=sb2.set)

        # 關閉（讓使用者有明確的出口）
        foot = ttk.Frame(adv)
        foot.pack(fill="x", padx=6, pady=(6, 10))
        ttk.Button(foot, text="關閉 / Close", command=self._close_advanced).pack(side="right")

    # -- 進階子視窗 --------------------------------------------------------- #
    def _sync_adv_label(self) -> None:
        arrow = "✕" if self._adv_shown else "▸"
        self.btn_adv.configure(text=f"⚙  進階設定 / Advanced   {arrow}")

    def _toggle_advanced(self) -> None:
        if self._adv_shown:
            self._close_advanced()
        else:
            self.open_advanced()

    def _close_advanced(self) -> None:
        if self._busy:
            messagebox.showinfo("仍在執行 / Still running",
                                "請等目前動作完成再關閉輸出視窗。\nPlease wait until the current action finishes.")
            return
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        if self.adv_win is not None:
            try:
                self.adv_win.destroy()
            except Exception:
                pass
        self.adv_win = None
        self.adv = None
        self._canvas = None
        self._adv_shown = False
        self._sync_adv_label()

    def open_advanced(self) -> None:
        """開啟獨立的「進階設定」視窗。

        獨立視窗可讓主視窗永遠維持精簡的一顆按鈕，內容再多也不會撐大主畫面。
        """
        if self.adv_win is not None:
            self.adv_win.lift()
            self.adv_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.adv_win = win
        win.title("進階設定 / Advanced — hermes-lean")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_advanced)

        # 內容可捲動：小螢幕或高縮放時仍能操作到最下面。
        canvas = tk.Canvas(win, highlightthickness=0, bd=0)
        self._canvas = canvas
        vbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        adv = ttk.Frame(canvas)
        self.adv = adv
        win_id = canvas.create_window((0, 0), window=adv, anchor="nw")
        self._build_advanced(adv)

        def _resize(_e=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win_id, width=canvas.winfo_width())

        adv.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        # 滑鼠滾輪（Windows / macOS 用 delta；Linux 另有 Button-4/5）
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        s = getattr(self, "s", 1.0)
        wl, wt, wr, wb = work_area(self.root)
        avail_w, avail_h = wr - wl, wb - wt
        # 標題列 + 視窗外框的高度（geometry 指的是客戶區，不含標題列）
        chrome = int(40 * s)

        # 兩段式量測：先給定寬度讓內容正確換行，再量真正需要的高度。
        # （若在寬度=1 時量測，所有標籤會擠成一直行，高度會被嚴重高估。）
        w = min(int(620 * s), max(600, avail_w - 80))
        win.geometry(f"{w}x{int(300 * s)}")
        win.update_idletasks()
        canvas.itemconfigure(win_id, width=max(1, canvas.winfo_width()))
        win.update_idletasks()

        need_w, need_h = self._content_size(win)
        w = min(max(need_w + 40, w), max(600, avail_w - 40))
        # 高度上限扣掉標題列，否則底部的「關閉」鈕會被工作列蓋住。
        h = min(max(need_h + 56, int(300 * s)), max(360, avail_h - chrome - 30))
        # 開在主視窗右邊，不要蓋住它；並確保整個視窗（含標題列）在工作區內。
        x = min(self.root.winfo_rootx() + self.root.winfo_width() + 12, max(wl, wr - w - 20))
        y = max(wt, min(self.root.winfo_rooty(), wb - h - chrome - 10))
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.minsize(min(w, int(460 * s)), min(h, int(280 * s)))
        canvas.configure(scrollregion=canvas.bbox("all"))

        self._adv_shown = True
        self._sync_adv_label()

    def _fit_main_window(self) -> None:
        """在內容確實改變時（例如狀態列首次出現）重新貼合主視窗。

        橫幅高度由 _lock_dynamic_heights() 鎖定，因此切換狀態文字不會改變版面，
        主視窗維持同一尺寸。
        """
        try:
            s = getattr(self, "s", 1.0)
            wl, wt, wr, wb = work_area(self.root)
            sw, sh = wr - wl, wb - wt
            need_w, need_h = self._content_size()
            w = min(max(need_w + 48, int(460 * s)), max(600, sw - 60))
            h = min(max(need_h + 64, int(180 * s)), max(360, sh - 90))
            self.root.minsize(w, h)
            self.root.geometry(f"{w}x{h}")
        except Exception:
            pass

    # -- 狀態卡 ------------------------------------------------------------- #
    def _paint_banner(self, kind: str) -> None:
        bg, fg, icon = STATES[kind]
        for w in (self.banner, self.banner.winfo_children()[0]):
            w.configure(bg=bg)
        for w in (self.banner_icon, self.banner_title, self.banner_detail):
            w.configure(bg=bg, fg=fg)
        self.banner_icon.configure(text=icon)

    def _set_state(self, kind: str, title: str, detail: str = "") -> None:
        self._paint_banner(kind)
        self.banner_title.configure(text=title)
        self.banner_detail.configure(text=detail)
        # 文字換行數改變 -> 橫幅高度改變 -> 立刻重新貼合主視窗。
        self.root.after_idle(self._fit_main_window)

    def _valid_root(self) -> Path | None:
        raw = self.hermes_root.get().strip()
        if not raw:
            return None
        p = Path(raw)
        if looks_like_checkout(p):
            return p
        if looks_like_checkout(p / "hermes-agent"):
            return p / "hermes-agent"
        return None

    def check_status(self) -> None:
        """自動判斷：現在需不需要按「一鍵套用」。"""
        if self._busy:
            return
        if self._valid_root() is None:
            self._set_state("error", "找不到 Hermes",
                            "請打開「進階設定」，選擇含 model_tools.py 的資料夾。\n"
                            "Open Advanced and pick the folder containing model_tools.py.")
            return
        self._set_state("busy", "檢查中… / Checking…", "")
        self._show_status_bar = False
        self._set_busy(True, "檢查中 / checking…")
        # Tk 變數只能從主執行緒讀取，因此先在這裡把命令組好再交給背景執行緒。
        cmd = self._command("check")
        threading.Thread(target=self._status_worker, args=(cmd,), daemon=True).start()

    def _status_worker(self, cmd: list[str]) -> None:
        try:
            proc = subprocess.run(cmd, cwd=str(HERE),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors="replace")
            out = proc.stdout or ""
            code = proc.returncode
        except Exception as e:
            self.root.after(0, self._set_state, "error", "檢查失敗 / Check failed", str(e))
            self.root.after(0, self._set_busy, False, "就緒 / Ready.")
            return

        self.root.after(0, self._log_block, out)
        if code != 0:
            self.root.after(0, self._set_state, "error", "設定有問題 / Config problem",
                            "請看下方輸出訊息，或按「進階設定」檢查。\nSee the output below.")
            self.root.after(0, self._set_busy, False, "需要處理 / needs attention")
            return

        changes = out.count("[change]")
        if changes:
            self.root.after(0, self._set_state, "todo",
                            "需要重新套用 / Needs re-apply",
                            f"有 {changes} 個檔案目前是原始版本（通常是剛更新完 Hermes）。\n"
                            f"{changes} file(s) are back to upstream (usually right after an update).\n"
                            "按上面的按鈕就能修好。 / Press the button above to fix it.")
            self.root.after(0, self._set_busy, False, "需要套用 / needs apply")
        else:
            self.root.after(0, self._set_state, "ok",
                            "一切正常 / All good",
                            "精簡描述已經套用好了，目前不需要做任何事。\n"
                            "Compact descriptions are applied. Nothing to do.")
            self.root.after(0, self._set_busy, False, "正常 / OK")

    # -- 小工具 ------------------------------------------------------------- #
    def say(self, text: str) -> None:
        if not hasattr(self, "log"):
            return
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_block(self, text: str) -> None:
        for line in text.splitlines():
            self.say(line)

    def refresh_root_status(self) -> None:
        p = Path(self.hermes_root.get().strip()) if self.hermes_root.get().strip() else None
        if p and looks_like_checkout(p):
            self.root_status.configure(text=f"✓ 有效的 Hermes 安裝位置 / Valid install  （{p}）", foreground="#137333")
        elif p and looks_like_checkout(p / "hermes-agent"):
            self.root_status.configure(text=f"✓ 有效的 Hermes 安裝位置  （{p / 'hermes-agent'}）", foreground="#137333")
        else:
            self.root_status.configure(text="✗ 還不是有效位置 / Not a valid install — 請選擇含 model_tools.py 的資料夾",
                                       foreground="#b00020")

    def _refresh_cfg_list(self) -> None:
        self.cfg_list.delete(0, "end")
        for p in self.extra_configs:
            self.cfg_list.insert("end", p)
        if not self.extra_configs:
            self.cfg_list.insert("end", "（目前沒有 / none — 可按「建立私有檔」）")
        self.cfg_list.configure(height=max(3, min(6, self.cfg_list.size())))

    def _refresh_skills(self) -> None:
        self.skill_list.delete(0, "end")
        for s in sorted(self._current_skills()):
            self.skill_list.insert("end", s)
        # 自動長高到剛好顯示完，避免最後一項被切掉（上限 10 行後改為捲動）。
        self.skill_list.configure(height=max(4, min(10, self.skill_list.size())))
        self.skill_hint.configure(text=f"儲存位置 / Saved to：{self._private_target()}")

    def _config_paths(self) -> list[Path]:
        paths = [Path(self.base_config.get().strip())] if self.base_config.get().strip() else []
        paths += [Path(p) for p in self.extra_configs]
        if hc is not None:
            dropin = HERE / "overrides.d"
            if dropin.is_dir():
                paths += sorted(p for p in dropin.glob("*.json") if not p.name.endswith(".example.json"))
        return paths

    def _current_skills(self) -> list[str]:
        skills: list[str] = []
        for p in self._config_paths():
            data = read_json(p)
            for s in data.get("skill_description_exception_skills") or []:
                if s not in skills:
                    skills.append(s)
        return skills

    def _load_gate(self) -> str:
        for p in self._config_paths():
            data = read_json(p)
            if data.get("skill_index_gate") in ("focus", "focus_and_coding"):
                return data["skill_index_gate"]
        return "focus"

    def _save_gate(self) -> None:
        target = self._private_target()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = read_json(target, strict=True) if target.exists() else {}
            data["_comment"] = data.get("_comment",
                                        "PRIVATE config layered on top of the base. Not for publishing.")
            data["skill_index_gate"] = self.gate.get()
            save_private_json(data)
            self.say(f"[i] 已儲存 精簡範圍 / index mode={self.gate.get()} -> {target}")
        except Exception as e:
            messagebox.showerror("儲存失敗 / Save failed", str(e))

    def _private_target(self) -> Path:
        """UI edits always go to the dedicated ignored file, never an imported config."""
        return DEFAULT_PRIVATE

    # -- 動作 --------------------------------------------------------------- #
    def on_autodetect(self) -> None:
        found = autodetect_root()
        if found:
            self.hermes_root.set(str(found))
            self.say(f"[i] 自動偵測到安裝位置 / auto-detected：{found}")
        else:
            messagebox.showwarning("找不到 / Not found",
                                   "無法自動偵測，請用「瀏覽…」自行指定。\n"
                                   "Auto-detect failed — please use Browse… to pick it yourself.")

    def on_browse_root(self) -> None:
        d = filedialog.askdirectory(title="選擇 hermes-agent 資料夾 / Select folder（內含 model_tools.py）")
        if d:
            self.hermes_root.set(d)

    def on_browse_base(self) -> None:
        f = filedialog.askopenfilename(title="選擇基礎設定檔 / Select base config JSON",
                                       filetypes=[("JSON", "*.json")],
                                       initialdir=str(DEFAULT_BASE.parent))
        if f and safe_config_path(Path(f)):
            self.base_config.set(f)

    def on_add_config(self) -> None:
        f = filedialog.askopenfilename(title="加入唯讀設定檔 / Add read-only config JSON",
                                       filetypes=[("JSON", "*.json")],
                                       initialdir=str(HERE))
        if f and safe_config_path(Path(f)) and f not in self.extra_configs:
            self.extra_configs.append(f)
            self._refresh_cfg_list()
            self._refresh_skills()

    def on_new_private(self) -> None:
        p = DEFAULT_PRIVATE
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                payload = {
                    "_comment": "PRIVATE config layered on top of the base. Not for publishing.",
                    "skill_description_exception_skills": [],
                }
                save_private_json(payload)
            else:
                read_json(p, strict=True)
            self._refresh_cfg_list()
            self._refresh_skills()
            self.say(f"[ok] 私有設定已就緒 / Private config ready: {p}")
        except Exception as exc:
            messagebox.showerror("建立失敗 / Create failed", str(exc))

    def on_open_config(self) -> None:
        sel = self.cfg_list.curselection()
        target = None
        if sel and not self.extra_configs:
            target = None
        elif sel:
            target = Path(self.extra_configs[sel[0]])
        if target is None:
            target = Path(self.base_config.get().strip())
        if target is None or not safe_config_path(target):
            messagebox.showinfo("開啟 / Open", "只能開啟存在的普通 .json 設定檔（不接受捷徑）。\nOnly an existing regular .json config can be opened.")
            return
        try:
            subprocess.Popen(editor_command(target), shell=False)
        except Exception as e:
            messagebox.showerror("開啟失敗 / Open failed", str(e))

    def on_remove_config(self) -> None:
        sel = self.cfg_list.curselection()
        if sel and self.extra_configs:
            del self.extra_configs[sel[0]]
            self._refresh_cfg_list()
            self._refresh_skills()

    def on_add_skill(self) -> None:
        name = self.skill_entry.get().strip()
        if name:
            cur = self._current_skills()
            if name not in cur:
                cur.append(name)
                self._write_skills(cur)
            self.skill_entry.delete(0, "end")

    def on_remove_skill(self) -> None:
        sel = self.skill_list.curselection()
        if not sel:
            return
        name = self.skill_list.get(sel[0])
        cur = [s for s in self._current_skills() if s != name]
        self._write_skills(cur)

    def _write_skills(self, skills: list[str]) -> None:
        target = self._private_target()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = read_json(target, strict=True) if target.exists() else {}
            data["_comment"] = data.get("_comment",
                                        "PRIVATE config layered on top of the base. Not for publishing.")
            data["skill_description_exception_skills"] = sorted(set(skills))
            save_private_json(data)
            self.say(f"[i] 已儲存白名單 / whitelist（{len(data['skill_description_exception_skills'])} 個 skill）-> {target}")
        except Exception as e:
            messagebox.showerror("儲存失敗 / Save failed", str(e))
        self._refresh_skills()

    def on_capture(self) -> None:
        if not messagebox.askyesno("擷取前確認 / Before capture", CAPTURE_WARNING):
            return
        f = filedialog.asksaveasfilename(title="擷取目前值到… / Capture current values to…", defaultextension=".json",
                                         initialdir=str(HERE), initialfile="captured.json",
                                         filetypes=[("JSON", "*.json")])
        if not f:
            return
        target = Path(f)
        if target.exists() and not messagebox.askyesno(
            "覆寫確認 / Confirm overwrite",
            f"檔案已存在，確定要先備份再覆寫？\nThe file exists. Back it up and overwrite?\n\n{target}",
        ):
            return
        self.run_action("capture", out=f)

    def _command(self, action: str, out: str | None = None) -> list[str]:
        root = self.hermes_root.get().strip()
        base = self.base_config.get().strip()
        cmd = [find_python(Path(root) if root else None), str(SCRIPT), "--hermes-root", root]
        if base:
            cmd += ["--config", base]
        for p in self.extra_configs:
            cmd += ["--add-config", p]
        if action == "check":
            cmd += ["--check"]
        elif action == "apply":
            cmd += ["--yes"]
        elif action == "restore":
            cmd += ["--restore"]
        elif action == "capture":
            cmd += ["--capture", out or "captured.json"]
        return cmd

    def run_action(self, action: str, out: str | None = None) -> None:
        if self._busy:
            return
        if self._valid_root() is None:
            messagebox.showwarning("請確認安裝位置 / Check install path",
                                   "請打開「進階設定」，選好 Hermes 安裝資料夾。\n"
                                   "Open Advanced and pick a valid Hermes install folder.")
            return
        if action == "apply":
            if not messagebox.askyesno("套用 / Apply",
                                       "要把精簡描述寫回 Hermes 安裝檔嗎？\n"
                                       "Write the compact descriptions back into the Hermes install?\n\n"
                                       "每個被改的檔案都會建立 .bak 備份。\n"
                                       "Every modified file gets a .bak backup."):
                return
        cmd = self._command(action, out)   # 主執行緒組命令
        self._show_status_bar = True
        self._set_busy(True, f"執行中 / running：{action}…")
        if action == "apply":
            self._set_state("busy", "套用中… / Applying…", "請稍等一下。 / Please wait a moment.")
        self.say("\n$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        self._current_action = action
        threading.Thread(target=self._run, args=(cmd, action), daemon=True).start()

    def _run(self, cmd: list[str], action: str) -> None:
        try:
            proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")
            self._proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                self.root.after(0, self.say, line.rstrip("\n"))
            code = proc.wait()
            self._proc = None
            self.root.after(0, self._done, action, code)
        except Exception as e:
            self._proc = None
            self.root.after(0, self.say, f"[x] {e}")
            self.root.after(0, self._done, action, 1)

    def _done(self, action: str, code: int) -> None:
        self._set_busy(False, f"完成 / done（結束碼 {code}）")
        self._current_action = ""
        if code == 0:
            messages = {
                "apply": ("套用完成 / Applied", "已安全寫回並驗證。請完整重開 Hermes（關閉後再開啟）才會生效。\nApplied and verified. Fully quit and relaunch Hermes to take effect."),
                "check": ("檢查完成 / Check complete", "檢查完成，未修改任何檔案。\nCheck completed; no files were changed."),
                "restore": ("還原完成 / Restored", "已還原上游原始碼。請完整重開 Hermes 才會生效。\nUpstream source restored. Fully quit and relaunch Hermes to take effect."),
                "capture": ("擷取完成 / Captured", "設定已擷取至你選擇的 JSON。\nSettings were captured to the selected JSON."),
            }
            title, detail = messages.get(action, ("完成 / Done", "動作已完成。 / Action completed."))
            self._set_state("ok", title, detail)
        else:
            self._set_state("error", "發生問題 / Something went wrong",
                            "請看下方輸出訊息。已自動打開「進階設定」。\n"
                            "See the output below — Advanced was opened for you.")
            if not self._adv_shown:
                self.open_advanced()

    def _set_busy(self, busy: bool, status: str) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_apply.configure(state=state)
        for name in ("btn_check", "btn_restore", "btn_capture"):
            b = getattr(self, name, None)
            if b is not None:
                b.configure(state=state)
        self.status.configure(text=status)
        # 只有「使用者主動執行的動作」才顯示底部狀態列；
        # 開窗自動檢查的結果已由上方橫幅呈現，避免重複兩行「檢查中」。
        if self._show_status_bar:
            self.status_frame.pack(fill="x", pady=(10, 0))
        else:
            self.status_frame.pack_forget()
        # 狀態列出現／消失也會改變所需高度，同步重新貼合。
        self.root.after_idle(self._fit_main_window)

    # -- 保存 --------------------------------------------------------------- #
    def on_close(self) -> None:
        if self._busy or (self._proc is not None and self._proc.poll() is None):
            messagebox.showwarning(
                "仍在執行 / Still running",
                "為避免中斷交易式寫入，請等目前動作完成再關閉。\n"
                "To avoid interrupting a transactional write, wait for the current action to finish.",
            )
            return
        save_state({
            "hermes_root": self.hermes_root.get().strip(),
            "base_config": self.base_config.get().strip(),
            "extra_configs": self.extra_configs,
        })
        self.root.destroy()


def main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    apply_scaling(root)
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    # 無介面煙霧測試用：HERMES_COMPACT_UI_AUTOCLOSE=<毫秒> 會在啟動後自動關閉。
    # 正常使用時沒有任何影響。
    autoclose = os.environ.get("HERMES_COMPACT_UI_AUTOCLOSE")
    if autoclose and autoclose.isdigit():
        root.after(int(autoclose), app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
