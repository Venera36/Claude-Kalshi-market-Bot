FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kalshi_bot ./kalshi_bot
CMD ["python", "-m", "kalshi_bot.main"]
