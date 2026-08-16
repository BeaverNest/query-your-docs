FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ static/
COPY scripts/ scripts/

ENV QYD_PORT=8020
ENV QYD_HOST=0.0.0.0
EXPOSE 8020

CMD ["python", "server.py"]
