FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 生产镜像只安装运行依赖。依赖文件不变时，后续代码更新会直接复用这一层。
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# 镜像保留一份可独立运行的代码；Compose 会用宿主机源码只读覆盖，
# 因而日常 Python 代码更新只需重启容器，不必重新构建依赖镜像。
COPY rollpig_cloud ./rollpig_cloud
COPY tools ./tools
RUN mkdir -p /app/static/resources

EXPOSE 8011

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/healthz', timeout=3)"

CMD ["uvicorn", "rollpig_cloud.main:app", "--host", "0.0.0.0", "--port", "8011"]
