# Security Policy / 安全政策

## Trust boundary / 信任邊界

Use hermes-lean only with a Hermes checkout you trust. Apply verification and capture import Python modules from the selected checkout. The GUI's automatic check uses the already-running interpreter and does not execute a selected checkout's virtual-environment executable.

只對你信任的 Hermes checkout 使用本工具。套用後驗證與擷取會載入所選 checkout 的 Python 模組；GUI 自動檢查則只使用目前已啟動的 Python，不執行所選資料夾中的虛擬環境。

## Built-in protections / 內建保護

- Strict JSON schema and non-finite-value rejection
- Python syntax preflight before any source write
- Symlink/reparse-point rejection for managed source targets
- Exclusive, collision-resistant backups
- Same-directory temporary files and atomic replacement
- All-target rollback on write or verification failure
- GUI-managed edits restricted to the Git-ignored `overrides.d/90-private.json`

## Reporting a vulnerability / 回報漏洞

Please use GitHub's private security-advisory feature rather than opening a public issue with exploit details. Do not include API keys, tokens, personal paths, private configs, memory files, or user profiles in a report.

請使用 GitHub 私密安全通報功能，不要在公開 Issue 張貼利用細節。回報中請勿包含 API Key、Token、個人路徑、私有設定、記憶檔或使用者資料。
