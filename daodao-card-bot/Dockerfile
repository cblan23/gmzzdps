FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-lock-linux.txt ./
RUN pip install --no-cache-dir -r requirements-lock-linux.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
CMD ["python", "-m", "app.supervisor"]
