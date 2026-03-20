# 目录结构

- `install.sh`：一键安装入口，支持 `bash <(curl ...)`
- `deploy.sh`：TGRC 控制入口与卸载/清理脚本
- `config.example.json`：配置模板
- `src/`：业务代码
- `src/tgr/`：核心模块
- `runtime/`：日志、session、数据库、备份
- `scripts/cleanup_legacy.sh`：旧版残留清理
