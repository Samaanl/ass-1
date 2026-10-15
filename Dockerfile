FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Leave DB_HOST empty to use the built-in SQLite file,
# or pass -e DB_HOST=<mysql-container-or-ip> to use MySQL.
ENV DB_PORT=3306 \
    DB_USER=root \
    DB_NAME=shop_db \
    PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "app.py"]
