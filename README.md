<div align="center">

<img src="https://capsule-render.vercel.app/api?type=rounded&height=220&color=0:EDEDED,45:CBD5E1,100:64748B&text=TG-Radar%20Plan%20C&fontSize=50&fontColor=111827&fontAlignY=40&desc=Minimal%20Premium%20Split%20Architecture%20for%20Long%20Running%20Telegram%20Radar&descAlignY=63" width="100%" />

<br/>
<br/>

<img src="https://img.shields.io/badge/Architecture-Core%20%2B%20Admin-111827?style=for-the-badge" />
<img src="https://img.shields.io/badge/Storage-SQLite%20WAL-334155?style=for-the-badge" />
<img src="https://img.shields.io/badge/Mode-Long%20Running-475569?style=for-the-badge" />
<img src="https://img.shields.io/badge/Runtime-Python%203.10%2B-64748B?style=for-the-badge" />

</div>

## Overview

This version is the full **Plan C** refactor of the original TG-Radar:

- `radar_core.py` only does message listening and alert delivery
- `radar_admin.py` only does ChatOps, folder sync, route queue and operational control
- dynamic state is moved into **SQLite** instead of stuffing everything into one `config.json`
- both processes are isolated and communicate through the database revision model

This structure is much safer for long-running use because the monitoring path and the management path no longer fight inside one giant process.

---

## Repository Layout

```text
TG-Radar-PlanC/
├─ config.example.json
├─ requirements.txt
├─ install.sh
├─ deploy.sh
├─ bootstrap_session.py
├─ radar_core.py
├─ radar_admin.py
├─ sync_once.py
├─ tgr/
│  ├─ config.py
│  ├─ db.py
│  ├─ logger.py
│  ├─ sync_logic.py
│  ├─ admin_service.py
│  ├─ core_service.py
│  ├─ telegram_utils.py
│  └─ version.py
└─ README.md
```

---

## Quick Start

```bash
git clone https://github.com/yourname/TG-Radar-PlanC.git
cd TG-Radar-PlanC
bash install.sh
```

Then:

1. edit `config.json`
2. run `python3 bootstrap_session.py`
3. run `TGR`
4. create systemd services and start them

---

## Telegram Commands

```text
-help
-ping
-status
-log 30
-folders
-rules 业务群
-enable 业务群
-disable 业务群
-addrule 业务群 核心词 苹果 华为
-delrule 业务群 核心词
-routes
-addroute 业务群 供需 担保
-delroute 业务群
-sync
-restart
-update
```

---

## Notes

- `config.json` only keeps credentials and a few global options
- `radar.db` is the runtime source of truth
- `radar_admin.py` manages ChatOps, sync and route queue
- `radar_core.py` focuses only on matching and alert delivery
- both services are systemd friendly and restartable

---

## Disclaimer

Use this repository only on Telegram accounts, groups and routing workflows you are authorized to operate.
