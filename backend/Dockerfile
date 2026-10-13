# 用 Debian slim 运行 FastAPI，并安装 mssql-python 所需的 Linux 系统库。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libltdl7 \
        libkrb5-3 \
        libgssapi-krb5-2 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY app ./app
COPY sql ./sql

EXPOSE 8000

# 配置文件在 Compose 运行时只读挂载，数据库凭据不会进入镜像。
# 应用配置维持本机地址；这里只把容器内 Uvicorn 监听地址覆盖为容器网卡。
CMD ["python", "-c", "import uvicorn; from app.main import app; uvicorn.run(app, host='0.0.0.0', port=app.state.settings.api_port)"]
