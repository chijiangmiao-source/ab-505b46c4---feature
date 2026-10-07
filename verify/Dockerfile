FROM python:3.12-alpine

WORKDIR /acceptance
# 应用源码一并打入，verify 先跑单元测试再做 HTTP 验收。
COPY app/ ./app/
COPY verify/verify.py ./verify.py

ENV APP_BASE_URL=http://app:8080 \
    APP_DIR=/acceptance/app

CMD ["python", "verify.py"]
