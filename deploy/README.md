# Web 部署版 · 部署与运行手册

对应：总纲 §12/§13（Web 部署版，127.0.0.1 环回 + SQLite 单机），spec-06 §2/§3。
代码仓库根目录 `web/`（前端）、`core/`（后端）、`deploy/`（部署样例）。

## 目录约定

```text
/opt/aat/
├── web/dist      # 前端构建产物（web/ → npm run build）
├── core/         # 后端源码 + venv/
│   └── data/     # SQLite 数据库、凭据、审计（运行时生成，勿提交）
└── deploy/       # 本目录样例（nginx.conf、systemd unit）
```

## 一、后端 core（FastAPI）

```bash
cd /workspace/core
python3 -m venv venv
venv/bin/pip install -r requirements.txt          # 运行依赖
venv/bin/python -m pytest -q                      # 开发自测
```

首次启动（未设 `CORE_AUTH_PASSWORD` 时自动生成随机口令）：

```bash
CORE_ENV=prod CORE_HOST=127.0.0.1 CORE_PORT=8001 venv/bin/uvicorn core.app:app --host 127.0.0.1 --port 8001
```

- 首启日志会打印一次性登录口令，并落盘 `core/data/credentials.json`（0600）。
- 若要指定口令：`CORE_AUTH_PASSWORD=你的强口令`，支持 `core auth set-password` 子命令后续修改。
- 健康检查：`curl -s http://127.0.0.1:8001/api/health`

### systemd 保活

```bash
sudo cp deploy/ai-agent-trading.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-agent-trading
sudo systemctl status ai-agent-trading
```

> core 内部还有进程级单实例锁（`core/data/single_instance.lock`，fcntl），重复拉起第二个进程会直接退出——与 systemd `Restart=always` 配合不会出现双实例。

## 二、前端 web（React + AntD5）

```bash
cd /workspace/web
npm install
npm run dev      # 开发：5173，Vite 反代 /api、/ws → 127.0.0.1:8001
npm run build    # 产物 dist/
```

## 三、Nginx 单入口（生产样式）

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/aat
sudo ln -s /etc/nginx/sites-available/aat /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

浏览器访问 `http://<主机>:8080`。生产环境请把监听改为对外 80/443 + TLS + 安全响应头。

## 四、备份

- SQLite WAL 在线备份：停写窗口执行 `sqlite3 core/data/aat.db ".backup 'backup.db'"`（spec-01 §3.8 提供 /api/backup 在线备份 API 的落地点在 P1 备份切片）。
- 审计表随库整体备份。

## 五、安全说明

- core 仅监听 127.0.0.1，对外一律经 Nginx 反代，避免 CORS；dev 模式才开放 CORS 到 `localhost:5173`。
- 登录失败 5 次/15 分钟按 ip:user 锁定；写操作双提交 Cookie CSRF；会话 Cookie HttpOnly + SameSite=Lax。
- `CORE_ENV=prod` 强制关闭 `/docs`、Cookie 增加 `Secure`。
