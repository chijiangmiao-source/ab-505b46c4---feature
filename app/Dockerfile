FROM python:3.12-alpine

WORKDIR /app
COPY . .
RUN chmod +x run.sh

ENV PORT=8080 \
    DB_PATH=/data/app.db

EXPOSE 8080

# 守护循环：进程退出即重启（用于「服务重启后重试」演练），数据落在挂载卷 /data。
CMD ["./run.sh"]
