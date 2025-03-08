FROM python:3.11

RUN apt-get update && apt-get install -y \
    libsqlite3-dev \
    iputils-ping \
    netcat-openbsd \
    sqlite3 \
    redis-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "hr_bot.py"]
