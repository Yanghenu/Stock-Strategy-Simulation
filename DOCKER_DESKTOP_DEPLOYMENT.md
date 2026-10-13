# QuantBacktest Docker Desktop 本机部署操作手册

本文说明如何将 QuantBacktest 前端和后端运行在本机 Docker Desktop 中，并继续使用本机 SQL Server 的 `QuantBacktest` 数据库。

## 1. 当前项目状态

截至 2026-10-13，本机 Docker Engine 29.8.2、Docker Compose v5.5.1 可用，SQL Server 正在监听 TCP 1433。项目已包含 Dockerfile、Compose 文件、Nginx 配置和 PowerShell 一键部署脚本。

本方案保留现有 SQL Server 和数据库，不把数据库迁移进容器。基金实验、股票纸面实验、回测档案和关注列表继续使用同一数据库。Redis 仅保存可重建的行情缓存。

| 组件 | 运行位置 | 连接方式 |
| --- | --- | --- |
| Vue 前端 | Docker 容器 | 浏览器访问 `http://localhost:5173` |
| FastAPI 后端 | Docker 容器 | 容器内监听 `0.0.0.0:8000` |
| Redis | 已有 Docker 容器 `redis` | 后端访问 `host.docker.internal:6379` |
| SQL Server | 当前 Windows 主机 | 后端访问 `host.docker.internal,1433` |

## 2. 部署前确认

1. 启动 Docker Desktop，确认 Docker Engine 处于运行状态。
2. 启动本机 SQL Server，确认数据库 `QuantBacktest` 存在、TCP 1433 可用，并已启用 SQL 登录。
3. 备份 `QuantBacktest` 数据库。首次切换时不要创建一个空数据库替代它。
4. 确认代理或防火墙允许后端容器访问 AKShare 所需的数据源。
5. 切换时先停止当前 Windows 终端中运行的前后端。特别是旧后端必须先停止，避免两个后端同时恢复同一批纸面实验任务。SQL Server 不要停止。

容器访问 Windows 主机服务时，使用 Docker Desktop 提供的 `host.docker.internal` 主机名，而不是容器内的 `localhost`。[Docker Desktop 网络文档](https://docs.docker.com/desktop/features/networking/networking-how-tos/)

## 3. 发布前需要的容器文件

在项目根目录准备以下文件：

| 文件 | 作用 |
| --- | --- |
| `backend/Dockerfile` | 安装 Python 依赖并启动 FastAPI；Linux 镜像包含 `mssql-python` 所需系统库 |
| `frontend/Dockerfile` | 构建 Vue 静态文件，并用 Nginx 对外提供页面 |
| `frontend/nginx.conf` | 提供前端静态文件，并将 `/api` 代理到 `http://backend:8000` |
| `compose.yaml` | 编排前端和后端，发布前端宿主机端口 `5173`；复用已存在的 Redis 容器 |
| `deploy-docker.ps1` | 一键检查配置、构建镜像、启动容器并验证页面和 API |
| `.dockerignore` | 排除虚拟环境、依赖缓存、构建产物和凭据文件 |
| `backend/config.docker.ini` | 本机私有容器配置，不能提交到版本库或写入镜像 |

容器内应用配置仍使用 `127.0.0.1` 以通过应用配置校验；Dockerfile 的 Uvicorn 启动命令单独将容器服务绑定到 `0.0.0.0`，供 Compose 网络中的前端访问。

`backend/config.docker.ini` 可以从现有的 `backend/config.ini` 复制，再修改以下配置。保留原配置中其他行情、费用和策略参数：

```ini
[api]
host = 127.0.0.1
port = 8000

[database]
server = host.docker.internal,1433
database = QuantBacktest
authentication = sql
username = <当前 SQL 登录用户名>
password = <当前 SQL 登录密码>

[redis]
host = host.docker.internal
port = 6379
```

将 `backend/config.docker.ini` 加入 `.gitignore`，并在后端 Compose 配置中只读挂载到容器内应用配置文件位置。不要把用户名和密码写进 Dockerfile、镜像层或公开的 Compose 文件。`mssql-python` 支持 Linux；Linux 容器构建需要安装其平台依赖。[Microsoft Python 驱动容器说明](https://learn.microsoft.com/en-us/sql/connect/python/mssql-python/container-local-development?view=sql-server-ver17)

Compose 中应满足以下要点：

- 前端把宿主机 `127.0.0.1:5173` 映射到容器 Nginx 的 `80` 端口。
- Nginx 将 `/api` 请求转发给 Compose 服务名 `backend` 的 `8000` 端口。Vite 的开发代理只用于开发服务器，生产容器需要 Nginx 代理。
- 后端通过 Docker Desktop 的 `host.docker.internal` 访问已存在且发布宿主机 `6379` 端口的 Redis 容器，不在本项目 Compose 中另建 Redis。
- 后端读取只读挂载的 Docker 配置；Docker 镜像构建上下文排除 `backend/config.ini` 和 `backend/config.docker.ini`。
- 前后端设置 `restart: unless-stopped`，便于 Docker Desktop 重启后恢复服务；Redis 容器仍由其原有 Compose 项目管理。

Docker Compose 的 `build` 配置负责从项目文件构建镜像，`up` 负责创建并启动服务。[Docker Compose Quickstart](https://docs.docker.com/compose/gettingstarted/)

## 4. 首次构建和启动

确保容器文件已准备好、Docker Desktop 正在运行，并已按第 2 节停止本机前后端。在 PowerShell 中切换到项目根目录：

```powershell
Set-Location 'D:\量化模拟'
docker compose config
docker compose up -d --build --remove-orphans
docker compose ps
```

也可以直接运行项目提供的一键脚本。脚本会检查 Linux 容器引擎、现有 Redis 容器及宿主机 `6379` 端口、SQL Server `1433`、Compose 配置和宿主机 `5173` 端口；首次运行时，它会从 `backend/config.ini` 生成被 Git 忽略的 `backend/config.docker.ini`，并将 Redis 地址设置为 `host.docker.internal`。启动时会移除本项目旧的孤立 `quantbacktest-redis` 容器，不会触碰独立的 `redis` 容器。脚本不会输出或写入镜像层的数据库密码。

```powershell
Set-Location 'D:\量化模拟'
.\deploy-docker.ps1
```

如果本机 PowerShell 执行策略阻止运行脚本，可仅对这次进程运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-docker.ps1
```

若宿主机 `5173` 仍被本机 Vite 占用，脚本会停止并提示；请在运行 Vite 的终端按 `Ctrl+C` 后重试。SQL Server 不需要停止。

`docker compose config` 检查 Compose 配置能否解析；`docker compose up -d --build --remove-orphans` 构建镜像并在后台启动前后端，同时移除旧的专属 Redis 容器。随后在 Docker Desktop 的 **Containers** 页面查看 QuantBacktest 前后端；原有 `redis` 容器由其自己的项目管理。

浏览器打开：

```text
http://localhost:5173
```

## 5. 部署后检查

先确认 QuantBacktest 的前后端容器均为运行状态：

```powershell
docker compose ps
```

查看后端启动日志：

```powershell
docker compose logs --tail 100 backend
```

跟踪实时日志：

```powershell
docker compose logs -f backend
```

确认页面和基金策略接口都可访问：

```powershell
Invoke-WebRequest http://localhost:5173/ -UseBasicParsing
Invoke-RestMethod http://localhost:5173/api/fund-paper/strategies
```

页面请求应返回 HTTP 200；基金策略接口应返回数据。也可以在 Docker Desktop 中选择后端容器的 **Logs** 查看启动错误。

## 6. 日常启停和更新

停止并移除 Compose 容器：

```powershell
docker compose down
```

重新启动已经创建的容器：

```powershell
docker compose start
```

修改前端或后端代码后重新构建并启动：

```powershell
docker compose up -d --build --remove-orphans
```

只重启后端：

```powershell
docker compose restart backend
```

不要随意使用 `docker compose down -v`。该选项会删除 Compose 卷；虽然本方案把重要实验数据保留在外部 SQL Server，仍应避免误删容器侧数据。Compose 卷的数据生命周期见 [Docker 卷文档](https://docs.docker.com/engine/storage/volumes/)。

## 7. 纸面实验与数据安全

- 切换前停止 Windows 上的旧后端，避免它与容器后端同时处理 SQL Server 中标记为运行中的任务。
- 容器后端必须连接原有 `QuantBacktest` 数据库。不要改成临时或新建的空数据库，否则页面看不到已有实验账本。
- SQL Server 保持在 Windows 主机运行；Docker Desktop 或后端容器重启不会删除 SQL Server 中的数据。
- 容器后端启动后，检查后端日志并查看纸面实验任务状态，确认运行中的任务已由容器后端接管。
- Redis 只用于行情缓存；缓存丢失后可以重新获取。实验交易、订单和持仓账本以 SQL Server 为准。

## 8. 常见问题

### `docker compose up` 提示端口已占用

宿主机 `5173` 正被当前 Vite 服务占用。先在运行 Vite 的终端按 `Ctrl+C`，再启动 Compose。若需要让本机开发服务和容器同时运行，可临时把 Compose 前端映射改为其他宿主机端口，例如 `5174:80`。

### 页面能打开，但 `/api` 返回 502 或 404

检查 Nginx 是否把 `/api` 转发到 `backend:8000`，并确认前端、后端服务处于同一个 Compose 网络。再查看 `docker compose logs --tail 100 backend`。

### 后端提示 SQL Server 不可用

确认容器配置的地址为 `host.docker.internal,1433`、数据库名为 `QuantBacktest`，账号密码正确；确认 Windows SQL Server 的 TCP 1433 监听正常。不要在容器配置中使用 `localhost`，它指向后端容器自身。

### Docker Desktop 中容器反复重启

运行 `docker compose ps` 和 `docker compose logs --tail 100 <服务名>`。优先检查 Docker 配置文件挂载路径、SQL Server 登录、已有 Redis 容器的 `6379` 端口发布和 Python 依赖安装日志。

## 9. 本机运行与镜像分享

在本机 Docker Desktop 中运行，执行第 4 节的 `docker compose up -d --build --remove-orphans` 即可，不需要发布到 Docker Hub。

若要让其他电脑下载镜像，还需为 Compose 服务设置镜像仓库名称、登录镜像仓库并推送镜像；每台目标电脑仍需要有可访问的 SQL Server 和自己的运行配置。推送镜像不会自动复制本机数据库或实验记录。[Compose 构建与推送配置](https://docs.docker.com/reference/compose-file/build/)
