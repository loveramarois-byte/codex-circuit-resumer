#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


APP_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "CodexCircuitResumer"
INSTALL_DIR = APP_HOME / "app"
CONTROL = INSTALL_DIR / "control.ps1"


def run_control(action):
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CONTROL), action],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode, (result.stdout + result.stderr).strip()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Codex 熔断续聊")
        self.geometry("760x520")
        self.minsize(680, 460)
        self.configure(bg="#f4f1ea")
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        self._build()
        self.refresh()
        self.after(5000, self.auto_refresh)

    def _build(self):
        header = tk.Frame(self, bg="#24211d", padx=24, pady=20)
        header.pack(fill="x")
        tk.Label(header, text="Codex 熔断续聊", fg="#fffaf0", bg="#24211d", font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        tk.Label(header, text="CC Switch 断线与模型满载后，自动逐档降级、续接并恢复原档位", fg="#d9d1c3", bg="#24211d").pack(anchor="w", pady=(6, 0))

        body = tk.Frame(self, bg="#f4f1ea", padx=24, pady=20)
        body.pack(fill="both", expand=True)
        self.state_text = tk.StringVar(value="正在读取…")
        tk.Label(body, textvariable=self.state_text, bg="#f4f1ea", fg="#2d2924", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")

        buttons = tk.Frame(body, bg="#f4f1ea")
        buttons.pack(fill="x", pady=16)
        for text, action in (("刷新状态", self.refresh), ("启动守望", lambda: self.action("start")), ("停止守望", lambda: self.action("stop")), ("开机自动运行", lambda: self.action("install"))):
            ttk.Button(buttons, text=text, command=action).pack(side="left", padx=(0, 10))

        self.details = tk.Text(body, wrap="word", relief="flat", bg="#fffdf8", fg="#38332d", padx=16, pady=14)
        self.details.pack(fill="both", expand=True)
        self.details.configure(state="disabled")
        tk.Label(body, text="档位规则：记住本对话原档位；极高 → 高 → 中 → 低，恢复后回到原档位。", bg="#f4f1ea", fg="#6d645a").pack(anchor="w", pady=(14, 0))

    def set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def action(self, name):
        code, output = run_control(name)
        if code:
            messagebox.showerror("操作失败", output or "未知错误")
        self.refresh()

    def refresh(self):
        code, output = run_control("status")
        if code:
            self.state_text.set("后台状态读取失败")
            self.set_details(output)
            return
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            self.state_text.set("后台状态异常")
            self.set_details(output)
            return
        healthy = data.get("healthy")
        paused = data.get("paused")
        self.state_text.set("运行正常" if healthy and not paused else "尚未运行或需要检查")
        details = [
            "当前状态：{}".format(data.get("phase", "未知")),
            "最后事件：{}".format(data.get("last_event", "暂无")),
            "待重试任务：{}".format(data.get("scheduled_retry_count", 0)),
            "正在续接：{}".format(data.get("inflight_count", 0)),
            "等待恢复原档位：{}".format(data.get("reasoning_restore_pending_count", 0)),
            "需人工处理：{}".format(data.get("blocked_count", 0)),
            "Codex CLI：{}".format(data.get("codex_binary") or "未找到"),
        ]
        self.set_details("\n\n".join(details))

    def auto_refresh(self):
        self.refresh()
        self.after(5000, self.auto_refresh)


if __name__ == "__main__":
    if not CONTROL.is_file():
        messagebox.showerror("安装不完整", "找不到 control.ps1，请重新运行 Install.ps1。")
        sys.exit(1)
    App().mainloop()
