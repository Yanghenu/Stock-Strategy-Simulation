# 量化策略模拟工作台

一个用于研究 A 股与基金行情、历史策略回测和纸面交易实验的本地应用。项目提供 Vue 前端和 FastAPI 后端，使用 AKShare 获取市场数据，使用 SQL Server 保存实验与回测记录，并以 Redis 缓存行情数据。

> 本项目仅用于行情研究和模拟交易，不连接券商，不会提交真实买卖订单。行情源可用性、数据质量、交易费用和撮合假设都会影响结果；模拟结果不代表未来表现。

## 功能

| 页面 | 内容 |
| --- | --- |
| 行情监控 | 按股票代码或名称查询行情，查看盘中分时、日线和关注标的。 |
| 策略回测 | 选择股票策略和参数，使用历史数据回测并查看收益、净值、回撤及成交记录。 |
| 纸面实验 | 创建多个股票模拟任务，跟踪任务策略、持仓、订单、盈亏和账户曲线。 |
| 回测档案 | 查看和比较已保存的历史回测结果。 |
| 基金行情 | 按代码或名称查询 ETF、LOF 行情和开放式基金净值。 |
| 基金实验 | 使用基金策略创建多个模拟任务并跟踪持仓和账户表现。场内基金按交易价格模拟，开放式基金按公布净值规则模拟。 |

股票回测和纸面实验遵循项目实现的 A 股交易约束，包括整手、T+1、佣金、印花税和滑点设置。基金和股票使用各自的实验策略与成交规则。所有实验账本保存在 SQL Server；Redis 只用于缓存行情，不作为实验记录的唯一存储。

## 技术栈

- 前端：Vue 3、TypeScript、Apache ECharts、Vite
- 后端：Python、FastAPI、AKShare
- 数据存储：SQL Server；Redis 用于行情缓存
- 容器部署：Docker Desktop、Docker Compose、Nginx

## 项目结构

```text
backend/                       FastAPI 服务、行情与策略、SQL 仓储及测试
  app/
  sql/                         数据库表结构
  tests/
  config.ini.example           本地配置模板
frontend/                      Vue 前端
  src/
compose.yaml                   前后端容器编排
deploy-docker.ps1              Docker Desktop 一键部署脚本
DOCKER_DESKTOP_DEPLOYMENT.md   Docker 部署详细说明
```

## Docker Desktop 部署

部署前请启动 Docker Desktop、SQL Server 和已有的 Redis 容器，并确认 `QuantBacktest` 数据库及登录方式可用。首次配置时，从模板创建本机配置并填写自己的数据库连接信息：

```powershell
if (-not (Test-Path backend/config.ini)) {
    Copy-Item backend/config.ini.example backend/config.ini
}
```

然后在项目根目录运行一键部署脚本：

```powershell
.\deploy-docker.ps1
```

部署完成后访问 <http://localhost:5173>。脚本会构建并启动前后端容器，复用已有 Redis 和 SQL Server。数据库凭据保存在本机配置文件中，不应写入镜像或提交到 Git。

更多准备步骤、检查方式、启停命令和故障排查见 [Docker Desktop 部署手册](DOCKER_DESKTOP_DEPLOYMENT.md)。

## 本地开发

### 环境要求

- Python 3.10 或更高版本
- Node.js 20.19 或更高版本
- 可连接的 SQL Server，且已创建 `QuantBacktest` 数据库
- Redis 服务（默认 `127.0.0.1:6379`）
- 可访问 AKShare 数据源的网络

应用启动时会在所配置的数据库中创建所需数据表；数据库本身需要预先创建。

### 配置后端

在项目根目录执行：

```powershell
if (-not (Test-Path backend/config.ini)) {
    Copy-Item backend/config.ini.example backend/config.ini
}
Set-Location backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m app.main
```

首次启动前，按本机环境编辑 `backend/config.ini` 中的 SQL Server、Redis 和其他配置。API 文档默认位于 <http://127.0.0.1:8000/docs>。

### 启动前端

在另一个 PowerShell 窗口，从项目根目录执行：

```powershell
Set-Location frontend
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 开发服务器会将 `/api` 请求转发给本机 FastAPI 服务。

## 配置和凭据

- `backend/config.ini.example` 是不含真实账号密码的模板。
- `backend/config.ini` 和 `backend/config.docker.ini` 用于本机运行，已由 `.gitignore` 排除；请勿提交真实数据库密码或其他访问凭据。
- 行情刷新频率、请求超时、数据库和 Redis 连接等配置应按本机环境调整。
- 实验、订单、持仓和回测记录以 SQL Server 为准；Redis 缓存可在服务重启后重新生成。

## 验证

后端单元测试：

```powershell
Set-Location backend
python -m unittest discover -s tests -v
```

前端类型检查与生产构建：

```powershell
Set-Location frontend
npm run build
```

单元测试使用固定行情和模拟数据库连接，不需要连接真实 SQL Server、Redis 或 AKShare。实际服务连通性请在本机另行确认。
